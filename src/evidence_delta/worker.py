from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, or_, select

from evidence_delta.database import Database
from evidence_delta.domain import build_timeline
from evidence_delta.models import (
    ArtifactDependencyRecord,
    ArtifactRecord,
    ArtifactVersionRecord,
    CaseRecord,
    ChangeKeyRecord,
    RecomputeJobRecord,
)
from evidence_delta.schemas import WorkerRunResult
from evidence_delta.service import EvidenceService


class SimulatedWorkerCrash(RuntimeError):
    pass


@dataclass(frozen=True)
class Computation:
    artifact_id: str
    artifact_key: str
    case_id: str
    case_revision: int
    payload: dict
    lineage: list[dict]
    input_fingerprint: str
    read_versions: dict[str, int]


class RecomputeWorker:
    def __init__(self, database: Database, lease_seconds: int = 30) -> None:
        self.database = database
        self.lease_seconds = lease_seconds
        self.service = EvidenceService(database)

    def _claim(self) -> RecomputeJobRecord | None:
        now = datetime.now(UTC)
        with self.database.session() as session, session.begin():
            job = session.scalar(
                select(RecomputeJobRecord)
                .where(
                    or_(
                        RecomputeJobRecord.status == "QUEUED",
                        (
                            (RecomputeJobRecord.status == "RUNNING")
                            & (RecomputeJobRecord.lease_until <= now)
                        ),
                    )
                )
                .order_by(RecomputeJobRecord.created_at, RecomputeJobRecord.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if job is None:
                return None
            job.status = "RUNNING"
            job.attempts += 1
            job.lease_until = now + timedelta(seconds=self.lease_seconds)
            job.last_error = None
            session.flush()
            session.expunge(job)
            return job

    def _compute(self, job: RecomputeJobRecord) -> Computation:
        with self.database.session() as session:
            artifact = session.get(ArtifactRecord, job.artifact_id)
            if artifact is None:
                raise KeyError(f"Unknown artifact: {job.artifact_id}")
            case_revision = session.scalar(
                select(CaseRecord.revision).where(CaseRecord.id == artifact.case_id)
            )
            change_version = session.scalar(
                select(ChangeKeyRecord.version).where(
                    ChangeKeyRecord.case_id == artifact.case_id,
                    ChangeKeyRecord.key == artifact.artifact_key,
                )
            )
            assertions = self.service.active_assertions_for_key(
                session, artifact.case_id, artifact.artifact_key
            )
            payload, lineage, fingerprint = build_timeline(artifact.artifact_key, assertions)
            return Computation(
                artifact_id=artifact.id,
                artifact_key=artifact.artifact_key,
                case_id=artifact.case_id,
                case_revision=int(case_revision or 0),
                payload=payload,
                lineage=lineage,
                input_fingerprint=fingerprint,
                read_versions={artifact.artifact_key: int(change_version or 0)},
            )

    def _publish(
        self,
        job: RecomputeJobRecord,
        computation: Computation,
        simulate_crash: bool,
    ) -> WorkerRunResult:
        with self.database.session() as session, session.begin():
            persisted_job = session.scalar(
                select(RecomputeJobRecord).where(RecomputeJobRecord.id == job.id).with_for_update()
            )
            artifact = session.scalar(
                select(ArtifactRecord)
                .where(ArtifactRecord.id == computation.artifact_id)
                .with_for_update()
            )
            if persisted_job is None or artifact is None:
                raise KeyError("Job or artifact disappeared during processing")

            existing = session.scalar(
                select(ArtifactVersionRecord).where(
                    ArtifactVersionRecord.artifact_id == artifact.id,
                    ArtifactVersionRecord.input_fingerprint == computation.input_fingerprint,
                )
            )

            if existing is None:
                latest_version = session.scalar(
                    select(func.max(ArtifactVersionRecord.version)).where(
                        ArtifactVersionRecord.artifact_id == artifact.id
                    )
                )
                version = ArtifactVersionRecord(
                    id=str(uuid4()),
                    artifact_id=artifact.id,
                    version=int(latest_version or 0) + 1,
                    computed_at_revision=computation.case_revision,
                    input_fingerprint=computation.input_fingerprint,
                    payload=computation.payload,
                    lineage=computation.lineage,
                )
                session.add(version)
                session.flush()
                for key, observed_version in computation.read_versions.items():
                    session.add(
                        ArtifactDependencyRecord(
                            id=str(uuid4()),
                            artifact_version_id=version.id,
                            change_key=key,
                            observed_version=observed_version,
                        )
                    )
            else:
                version = existing

            artifact.current_version_id = version.id
            persisted_job.status = "SUCCEEDED"
            persisted_job.lease_until = None
            session.flush()

            if simulate_crash:
                raise SimulatedWorkerCrash(
                    "Worker terminated after writes but before transaction commit"
                )

            return WorkerRunResult(
                claimed=True,
                job_id=job.id,
                artifact_id=artifact.id,
                artifact_key=artifact.artifact_key,
                version=version.version,
            )

    def run_once(self, simulate_crash: bool = False) -> WorkerRunResult:
        job = self._claim()
        if job is None:
            return WorkerRunResult(claimed=False)
        computation = self._compute(job)
        return self._publish(job, computation, simulate_crash=simulate_crash)

    def run_until_idle(self, max_jobs: int = 10_000) -> list[WorkerRunResult]:
        results: list[WorkerRunResult] = []
        for _ in range(max_jobs):
            result = self.run_once()
            if not result.claimed:
                return results
            results.append(result)
        raise RuntimeError(f"Worker did not become idle after {max_jobs} jobs")
