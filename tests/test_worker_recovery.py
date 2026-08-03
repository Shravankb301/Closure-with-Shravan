from __future__ import annotations

import pytest
from sqlalchemy import func, select

from evidence_delta.models import (
    ArtifactDependencyRecord,
    ArtifactRecord,
    ArtifactVersionRecord,
    AssertionRecord,
    DocumentRecord,
    RecomputeJobRecord,
)
from evidence_delta.service import EvidenceService
from evidence_delta.worker import RecomputeWorker, SimulatedWorkerCrash
from tests.helpers import document


def test_worker_killed_before_commit_retries_without_orphans(
    service: EvidenceService,
    worker: RecomputeWorker,
) -> None:
    case = service.create_case("Crash recovery case")
    added = service.ingest_document(case.id, document(1, "entity-1", 3))

    with pytest.raises(SimulatedWorkerCrash):
        worker.run_once(simulate_crash=True)

    with service.database.session() as session:
        assert session.scalar(select(func.count(ArtifactVersionRecord.id))) == 0
        assert session.scalar(select(func.count(ArtifactDependencyRecord.id))) == 0
        job = session.scalar(select(RecomputeJobRecord))
        assert job is not None
        assert job.status == "RUNNING"
        assert job.attempts == 1

    retry = worker.run_once()
    assert retry.claimed is True
    assert retry.version == 1

    with service.database.session() as session:
        versions = session.scalars(select(ArtifactVersionRecord)).all()
        dependencies = session.scalars(select(ArtifactDependencyRecord)).all()
        artifact = session.scalar(select(ArtifactRecord))
        job = session.scalar(select(RecomputeJobRecord))
        assert len(versions) == 1
        assert len(dependencies) == 1
        assert artifact is not None
        assert artifact.current_version_id == versions[0].id
        assert dependencies[0].artifact_version_id == versions[0].id
        assert dependencies[0].change_key == "timeline:entity-1:2026-03-03"
        assert dependencies[0].observed_version == 1
        assert versions[0].lineage == [
            {
                "assertion_id": versions[0].payload["events"][0]["assertion_id"],
                "document_id": added.document_id,
                "source_locator": "record:1",
                "source_text": "Event 1 for entity-1",
                "time_precision": "EXACT",
            }
        ]
        assert job is not None
        assert job.status == "SUCCEEDED"
        assert job.attempts == 2


def test_duplicate_document_does_not_duplicate_work(
    service: EvidenceService,
    worker: RecomputeWorker,
) -> None:
    case = service.create_case("Idempotency case")
    payload = document(1, "entity-1", 3)

    first = service.ingest_document(case.id, payload)
    renamed = payload.model_copy(update={"filename": "renamed.json"})
    second = service.ingest_document(case.id, renamed)

    assert second.deduplicated is True
    assert second.document_id == first.document_id
    worker.run_until_idle()

    with service.database.session() as session:
        assert session.scalar(select(func.count(DocumentRecord.id))) == 1
        assert session.scalar(select(func.count(AssertionRecord.id))) == 1
        assert session.scalar(select(func.count(RecomputeJobRecord.id))) == 1


def test_document_idempotency_ignores_assertion_order(
    service: EvidenceService,
) -> None:
    case = service.create_case("Canonical idempotency")
    payload = document(1, "entity-1", 3)
    second_assertion = payload.assertions[0].model_copy(
        update={"source_locator": "record:2", "source_text": "Second source span"}
    )
    ordered = payload.model_copy(update={"assertions": [payload.assertions[0], second_assertion]})
    reordered = payload.model_copy(update={"assertions": [second_assertion, payload.assertions[0]]})

    first = service.ingest_document(case.id, ordered)
    second = service.ingest_document(case.id, reordered)

    assert second.deduplicated is True
    assert second.document_id == first.document_id
