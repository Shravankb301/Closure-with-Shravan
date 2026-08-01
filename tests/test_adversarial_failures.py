from __future__ import annotations

import pytest
from sqlalchemy import func, select

from evidence_delta.models import (
    ArtifactDependencyRecord,
    ArtifactVersionRecord,
    RecomputeJobRecord,
)
from evidence_delta.service import EvidenceService
from evidence_delta.worker import (
    RecomputeWorker,
    RetryableComputationError,
    SimulatedWorkerCrash,
)
from tests.helpers import document


def test_dependency_change_between_compute_and_publish_supersedes_stale_result(
    service: EvidenceService,
    worker: RecomputeWorker,
) -> None:
    case = service.create_case("Publication race")
    service.ingest_document(case.id, document(1, "entity-1", 3))
    worker.run_until_idle()

    service.ingest_document(case.id, document(2, "entity-1", 3))

    def concurrent_mutation(_computation) -> None:
        service.ingest_document(case.id, document(3, "entity-1", 3))

    stale = worker.run_once(before_publish=concurrent_mutation)
    assert stale.claimed is True
    assert stale.published is False
    assert stale.reason == "dependency_advanced"

    published = worker.run_until_idle()
    assert len([result for result in published if result.published]) == 1
    assert service.incremental_state(case.id) == service.full_rebuild_state(case.id)

    with service.database.session() as session:
        statuses = session.scalars(
            select(RecomputeJobRecord.status).order_by(RecomputeJobRecord.target_revision)
        ).all()
        assert statuses == ["SUCCEEDED", "SUPERSEDED", "SUCCEEDED"]
        # The rejected computation never became an artifact version.
        assert session.scalar(select(func.count(ArtifactVersionRecord.id))) == 2


def test_reader_can_distinguish_stale_artifact_while_recompute_is_pending(
    service: EvidenceService,
    worker: RecomputeWorker,
) -> None:
    case = service.create_case("Visible freshness")
    service.ingest_document(case.id, document(1, "entity-1", 3))
    worker.run_until_idle()

    service.ingest_document(case.id, document(2, "entity-1", 3))
    pending = service.current_artifact(case.id, "timeline:entity-1:2026-03-03")
    assert pending is not None
    assert pending["fresh"] is False
    assert pending["dependency_versions"] == [
        {"key": "timeline:entity-1:2026-03-03", "observed": 1, "current": 2}
    ]

    worker.run_until_idle()
    current = service.current_artifact(case.id, "timeline:entity-1:2026-03-03")
    assert current is not None
    assert current["fresh"] is True


def test_nonretryable_poison_job_fails_once_without_logging_evidence(
    service: EvidenceService,
) -> None:
    case = service.create_case("Poison job")
    service.ingest_document(case.id, document(1, "entity-1", 3))

    class PoisonWorker(RecomputeWorker):
        def _compute(self, job):
            raise ValueError("sensitive source text must never reach the job record")

    worker = PoisonWorker(service.database, lease_seconds=0, max_attempts=3)
    results = worker.run_until_idle()

    assert [result.reason for result in results] == ["failed_permanent"]
    with service.database.session() as session:
        job = session.scalar(select(RecomputeJobRecord))
        assert job is not None
        assert job.status == "FAILED_PERMANENT"
        assert job.attempts == 1
        assert job.last_error == "ValueError"
        assert "sensitive" not in job.last_error


def test_retryable_failure_stops_at_attempt_budget(service: EvidenceService) -> None:
    case = service.create_case("Transient failure budget")
    service.ingest_document(case.id, document(1, "entity-1", 3))

    class UnavailableWorker(RecomputeWorker):
        def _compute(self, job):
            raise RetryableComputationError("sensitive transient detail")

    worker = UnavailableWorker(service.database, lease_seconds=0, max_attempts=3)
    results = worker.run_until_idle()

    assert [result.reason for result in results] == [
        "retry_queued",
        "retry_queued",
        "failed_permanent",
    ]
    with service.database.session() as session:
        job = session.scalar(select(RecomputeJobRecord))
        assert job is not None
        assert job.status == "FAILED_PERMANENT"
        assert job.attempts == 3
        assert job.last_error == "RetryableComputationError"


def test_repeated_process_death_eventually_exhausts_the_lease_budget(
    service: EvidenceService,
) -> None:
    case = service.create_case("Repeated process death")
    service.ingest_document(case.id, document(1, "entity-1", 3))
    worker = RecomputeWorker(service.database, lease_seconds=0, max_attempts=2)

    for _ in range(2):
        with pytest.raises(SimulatedWorkerCrash):
            worker.run_once(simulate_crash=True)

    assert worker.run_once().claimed is False
    with service.database.session() as session:
        job = session.scalar(select(RecomputeJobRecord))
        assert job is not None
        assert job.status == "FAILED_PERMANENT"
        assert job.attempts == 2
        assert job.last_error == "LeaseExpired"
        assert session.scalar(select(func.count(ArtifactVersionRecord.id))) == 0


def test_expired_worker_cannot_overwrite_new_claim_owner(service: EvidenceService) -> None:
    case = service.create_case("Fenced lease")
    service.ingest_document(case.id, document(1, "entity-1", 3))
    stale_worker = RecomputeWorker(service.database, lease_seconds=0)
    replacement_worker = RecomputeWorker(service.database, lease_seconds=0)
    replacement_results = []

    def replace_expired_claim(_computation) -> None:
        replacement_results.append(replacement_worker.run_once())

    stale_result = stale_worker.run_once(before_publish=replace_expired_claim)

    assert replacement_results[0].published is True
    assert stale_result.published is False
    assert stale_result.reason == "claim_lost"
    with service.database.session() as session:
        job = session.scalar(select(RecomputeJobRecord))
        assert job is not None
        assert job.status == "SUCCEEDED"
        assert job.attempts == 2
        assert job.claim_token is None
        assert session.scalar(select(func.count(ArtifactVersionRecord.id))) == 1


def test_equivalent_state_cycle_appends_fresh_dependency_observation(
    service: EvidenceService,
    worker: RecomputeWorker,
) -> None:
    case = service.create_case("Equivalent state cycle")
    first = service.ingest_document(case.id, document(1, "entity-1", 3))
    worker.run_until_idle()
    second = service.ingest_document(case.id, document(2, "entity-1", 3))
    worker.run_until_idle()

    service.retract_document(case.id, second.document_id, "duplicate witness withdrawn")
    worker.run_until_idle()

    artifact = service.current_artifact(case.id, "timeline:entity-1:2026-03-03")
    assert artifact is not None
    assert artifact["version"] == 3
    assert artifact["fresh"] is True
    assert artifact["payload"]["events"][0]["assertion_id"] is not None
    assert artifact["lineage"][0]["document_id"] == first.document_id
    with service.database.session() as session:
        assert session.scalar(select(func.count(ArtifactVersionRecord.id))) == 3
        latest_dependency = session.scalar(
            select(ArtifactDependencyRecord)
            .join(
                ArtifactVersionRecord,
                ArtifactVersionRecord.id == ArtifactDependencyRecord.artifact_version_id,
            )
            .where(ArtifactVersionRecord.version == 3)
        )
        assert latest_dependency is not None
        assert latest_dependency.observed_version == 3
