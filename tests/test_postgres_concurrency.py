from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import pytest
from sqlalchemy import func, select

from evidence_delta.database import Database
from evidence_delta.models import (
    ArtifactVersionRecord,
    AssertionRecord,
    DocumentRecord,
    RecomputeJobRecord,
)
from evidence_delta.service import EvidenceService
from evidence_delta.worker import RecomputeWorker
from tests.helpers import document


@pytest.fixture
def postgres_database() -> Database:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL concurrency tests")
    database = Database(url)
    database.drop_schema()
    database.create_schema()
    yield database
    database.drop_schema()


def test_concurrent_duplicate_ingestion_returns_one_logical_document(
    postgres_database: Database,
) -> None:
    service = EvidenceService(postgres_database)
    case = service.create_case("Concurrent idempotency")
    payload = document(1, "entity-1", 3)
    barrier = Barrier(2)

    def upload() -> tuple[str, bool]:
        barrier.wait()
        result = service.ingest_document(case.id, payload)
        return result.document_id, result.deduplicated

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: upload(), range(2)))

    assert len({document_id for document_id, _ in results}) == 1
    assert sorted(deduplicated for _, deduplicated in results) == [False, True]
    with postgres_database.session() as session:
        assert session.scalar(select(func.count(DocumentRecord.id))) == 1
        assert session.scalar(select(func.count(AssertionRecord.id))) == 1
        assert session.scalar(select(func.count(RecomputeJobRecord.id))) == 1


def test_four_workers_claim_each_postgres_job_once(postgres_database: Database) -> None:
    service = EvidenceService(postgres_database)
    case = service.create_case("Concurrent workers")
    for sequence in range(20):
        service.ingest_document(
            case.id,
            document(sequence, f"entity-{sequence}", (sequence % 10) + 1),
        )

    claimed_job_ids: list[str] = []
    claimed_lock = Lock()
    barrier = Barrier(4)

    def drain() -> None:
        local_worker = RecomputeWorker(postgres_database)
        barrier.wait()
        while True:
            result = local_worker.run_once()
            if not result.claimed:
                return
            if result.job_id is not None:
                with claimed_lock:
                    claimed_job_ids.append(result.job_id)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _index: drain(), range(4)))

    assert len(claimed_job_ids) == 20
    assert len(set(claimed_job_ids)) == 20
    with postgres_database.session() as session:
        assert session.scalar(select(func.count(ArtifactVersionRecord.id))) == 20
        jobs = session.scalars(select(RecomputeJobRecord)).all()
        assert {job.status for job in jobs} == {"SUCCEEDED"}
        assert {job.attempts for job in jobs} == {1}


def test_postgres_fences_worker_after_lease_takeover(postgres_database: Database) -> None:
    service = EvidenceService(postgres_database)
    case = service.create_case("PostgreSQL fenced lease")
    service.ingest_document(case.id, document(1, "entity-1", 3))
    stale_worker = RecomputeWorker(postgres_database, lease_seconds=0)
    replacement_worker = RecomputeWorker(postgres_database, lease_seconds=0)
    replacement_results = []

    def replace_expired_claim(_computation) -> None:
        replacement_results.append(replacement_worker.run_once())

    stale_result = stale_worker.run_once(before_publish=replace_expired_claim)

    assert replacement_results[0].published is True
    assert stale_result.reason == "claim_lost"
    with postgres_database.session() as session:
        job = session.scalar(select(RecomputeJobRecord))
        assert job is not None
        assert job.status == "SUCCEEDED"
        assert job.attempts == 2
        assert session.scalar(select(func.count(ArtifactVersionRecord.id))) == 1
