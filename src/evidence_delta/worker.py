from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.exc import OperationalError

from evidence_delta.database import Database
from evidence_delta.domain import build_timeline
from evidence_delta.models import (
    ArtifactDependencyRecord,
    ArtifactRecord,
    ArtifactVersionRecord,
    ChangeKeyRecord,
    RecomputeJobRecord,
)
from evidence_delta.schemas import WorkerRunResult
from evidence_delta.service import EvidenceService


class SimulatedWorkerCrash(RuntimeError):
    pass


class SupersededComputation(RuntimeError):
    pass


class RetryableComputationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Computation:
    artifact_id: str
    artifact_key: str
    case_id: str
    trigger_revision: int
    payload: dict
    lineage: list[dict]
    input_fingerprint: str
    read_versions: dict[str, int]


class RecomputeWorker:
    def __init__(
        self,
        database: Database,
        lease_seconds: int = 30,
        max_attempts: int = 3,
    ) -> None:
        self.database = database
        if lease_seconds < 0:
            raise ValueError("lease_seconds must be non-negative")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.service = EvidenceService(database)

    def _expire_exhausted_leases(self, session, now: datetime) -> None:
        exhausted = session.scalars(
            select(RecomputeJobRecord)
            .where(
                RecomputeJobRecord.status == "RUNNING",
                RecomputeJobRecord.lease_until <= now,
                RecomputeJobRecord.attempts >= self.max_attempts,
            )
            .with_for_update(skip_locked=True)
        ).all()
        for job in exhausted:
            job.status = "FAILED_PERMANENT"
            job.lease_until = None
            job.claim_token = None
            job.last_error = "LeaseExpired"

    @staticmethod
    def _database_now(session) -> datetime:
        """Use database time so worker clock skew cannot steal a live lease."""

        now = session.scalar(select(func.current_timestamp()))
        if now is None:
            raise RuntimeError("DatabaseClockUnavailable")
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        return now

    def _claim(self) -> RecomputeJobRecord | None:
        with self.database.session() as session, session.begin():
            now = self._database_now(session)
            self._expire_exhausted_leases(session, now)
            job = session.scalar(
                select(RecomputeJobRecord)
                .where(
                    RecomputeJobRecord.attempts < self.max_attempts,
                    or_(
                        RecomputeJobRecord.status == "QUEUED",
                        (
                            (RecomputeJobRecord.status == "RUNNING")
                            & (RecomputeJobRecord.lease_until <= now)
                        ),
                    ),
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
            job.claim_token = str(uuid4())
            job.last_error = None
            session.flush()
            session.expunge(job)
            return job

    def _compute(self, job: RecomputeJobRecord) -> Computation:
        with self.database.session() as session, session.begin():
            artifact = session.get(ArtifactRecord, job.artifact_id)
            if artifact is None:
                raise KeyError(f"Unknown artifact: {job.artifact_id}")
            first_change_version = session.scalar(
                select(ChangeKeyRecord.version).where(
                    ChangeKeyRecord.case_id == artifact.case_id,
                    ChangeKeyRecord.key == artifact.artifact_key,
                )
            )
            if first_change_version is None:
                raise RuntimeError("MissingChangeKey")
            if first_change_version != job.target_revision:
                raise SupersededComputation("The job no longer owns this key version")

            assertions = self.service.active_assertions_for_key(
                session, artifact.case_id, artifact.artifact_key
            )
            second_change_version = session.scalar(
                select(ChangeKeyRecord.version).where(
                    ChangeKeyRecord.case_id == artifact.case_id,
                    ChangeKeyRecord.key == artifact.artifact_key,
                )
            )
            if second_change_version != first_change_version:
                raise SupersededComputation("Dependency advanced while inputs were read")

            payload, lineage, fingerprint = build_timeline(artifact.artifact_key, assertions)
            return Computation(
                artifact_id=artifact.id,
                artifact_key=artifact.artifact_key,
                case_id=artifact.case_id,
                trigger_revision=job.target_revision,
                payload=payload,
                lineage=lineage,
                input_fingerprint=fingerprint,
                read_versions={artifact.artifact_key: int(first_change_version)},
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
            if persisted_job is None:
                raise KeyError("Job disappeared during processing")
            if not self._owns_claim(persisted_job, job):
                return self._claim_lost(job, computation.artifact_key)

            observed_keys = sorted(computation.read_versions)
            current_versions = dict(
                session.execute(
                    select(ChangeKeyRecord.key, ChangeKeyRecord.version)
                    .where(
                        ChangeKeyRecord.case_id == computation.case_id,
                        ChangeKeyRecord.key.in_(observed_keys),
                    )
                    .order_by(ChangeKeyRecord.key)
                    .with_for_update()
                ).all()
            )
            if current_versions != computation.read_versions:
                persisted_job.status = "SUPERSEDED"
                persisted_job.lease_until = None
                persisted_job.claim_token = None
                persisted_job.last_error = "DependencyAdvanced"
                return WorkerRunResult(
                    claimed=True,
                    published=False,
                    job_id=job.id,
                    artifact_id=computation.artifact_id,
                    artifact_key=computation.artifact_key,
                    reason="dependency_advanced",
                )

            artifact = session.scalar(
                select(ArtifactRecord)
                .where(ArtifactRecord.id == computation.artifact_id)
                .with_for_update()
            )
            if artifact is None:
                raise KeyError("Artifact disappeared during processing")

            existing = session.scalar(
                select(ArtifactVersionRecord).where(
                    ArtifactVersionRecord.source_job_id == job.id,
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
                    source_job_id=job.id,
                    version=int(latest_version or 0) + 1,
                    computed_at_revision=computation.trigger_revision,
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
            persisted_job.claim_token = None
            session.flush()

            if simulate_crash:
                raise SimulatedWorkerCrash(
                    "Worker terminated after writes but before transaction commit"
                )

            return WorkerRunResult(
                claimed=True,
                published=True,
                job_id=job.id,
                artifact_id=artifact.id,
                artifact_key=artifact.artifact_key,
                version=version.version,
            )

    @staticmethod
    def _owns_claim(persisted_job: RecomputeJobRecord, claimed_job: RecomputeJobRecord) -> bool:
        return (
            persisted_job.status == "RUNNING"
            and persisted_job.claim_token is not None
            and persisted_job.claim_token == claimed_job.claim_token
        )

    @staticmethod
    def _claim_lost(job: RecomputeJobRecord, artifact_key: str | None = None) -> WorkerRunResult:
        return WorkerRunResult(
            claimed=True,
            published=False,
            job_id=job.id,
            artifact_id=job.artifact_id,
            artifact_key=artifact_key,
            reason="claim_lost",
        )

    def _mark_superseded(self, job: RecomputeJobRecord, reason: str) -> WorkerRunResult:
        with self.database.session() as session, session.begin():
            persisted = session.scalar(
                select(RecomputeJobRecord).where(RecomputeJobRecord.id == job.id).with_for_update()
            )
            if persisted is None:
                raise KeyError(f"Missing superseded job: {job.id}")
            if not self._owns_claim(persisted, job):
                return self._claim_lost(job)
            persisted.status = "SUPERSEDED"
            persisted.lease_until = None
            persisted.claim_token = None
            persisted.last_error = "DependencyAdvanced"
        return WorkerRunResult(
            claimed=True,
            published=False,
            job_id=job.id,
            artifact_id=job.artifact_id,
            reason=reason,
        )

    def _record_failure(self, job: RecomputeJobRecord, error: Exception) -> WorkerRunResult:
        with self.database.session() as session, session.begin():
            persisted = session.scalar(
                select(RecomputeJobRecord).where(RecomputeJobRecord.id == job.id).with_for_update()
            )
            if persisted is None:
                raise KeyError(f"Missing failed job: {job.id}")
            if not self._owns_claim(persisted, job):
                return self._claim_lost(job)
            retryable = isinstance(
                error,
                (RetryableComputationError, OperationalError, TimeoutError, ConnectionError),
            )
            permanent = not retryable or persisted.attempts >= self.max_attempts
            persisted.status = "FAILED_PERMANENT" if permanent else "QUEUED"
            persisted.lease_until = None
            persisted.claim_token = None
            # Store only the exception class. Error messages may contain evidence.
            persisted.last_error = type(error).__name__
        return WorkerRunResult(
            claimed=True,
            published=False,
            job_id=job.id,
            artifact_id=job.artifact_id,
            reason="failed_permanent" if permanent else "retry_queued",
        )

    def run_once(
        self,
        simulate_crash: bool = False,
        before_publish: Callable[[Computation], None] | None = None,
    ) -> WorkerRunResult:
        job = self._claim()
        if job is None:
            return WorkerRunResult(claimed=False)
        try:
            computation = self._compute(job)
            if before_publish is not None:
                before_publish(computation)
            return self._publish(job, computation, simulate_crash=simulate_crash)
        except SimulatedWorkerCrash:
            raise
        except SupersededComputation:
            return self._mark_superseded(job, "dependency_advanced")
        except Exception as error:
            return self._record_failure(job, error)

    def run_until_idle(self, max_jobs: int = 10_000) -> list[WorkerRunResult]:
        results: list[WorkerRunResult] = []
        for _ in range(max_jobs):
            result = self.run_once()
            if not result.claimed:
                return results
            results.append(result)
        raise RuntimeError(f"Worker did not become idle after {max_jobs} jobs")
