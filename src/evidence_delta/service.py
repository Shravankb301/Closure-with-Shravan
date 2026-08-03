from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import and_, exists, func, select
from sqlalchemy.orm import Session

from evidence_delta.analysis import ActiveEvent, derive_findings
from evidence_delta.database import Database
from evidence_delta.domain import (
    AssertionView,
    build_timeline,
    canonical_json,
    parse_timeline_key,
    sha256_json,
    timeline_key,
)
from evidence_delta.models import (
    ArtifactDependencyRecord,
    ArtifactRecord,
    ArtifactVersionRecord,
    AssertionRecord,
    CaseRecord,
    ChangeKeyRecord,
    ChangeSetRecord,
    DocumentRecord,
    DocumentRetractionRecord,
    RecomputeJobRecord,
)
from evidence_delta.schemas import CaseAssignmentInput, DocumentInput, MutationResult


def new_id() -> str:
    return str(uuid4())


class EvidenceService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create_case(self, name: str) -> CaseRecord:
        with self.database.session() as session, session.begin():
            record = CaseRecord(id=new_id(), name=name, revision=0)
            session.add(record)
        return record

    def assign_case(self, case_id: str, assignment: CaseAssignmentInput) -> dict:
        with self.database.session() as session, session.begin():
            case = session.scalar(
                select(CaseRecord).where(CaseRecord.id == case_id).with_for_update()
            )
            if case is None:
                raise KeyError(f"Unknown case: {case_id}")
            values = assignment.model_dump()
            case.assigned_officer = values["assigned_officer"]
            case.assigned_badge = values["assigned_badge"]
            case.assigned_unit = values["assigned_unit"]
            case.handoff_note = values["handoff_note"]
            session.flush()
            return {
                "case_id": case.id,
                "assigned_officer": case.assigned_officer,
                "assigned_badge": case.assigned_badge,
                "assigned_unit": case.assigned_unit,
                "handoff_note": case.handoff_note,
            }

    @staticmethod
    def _document_hash(document: DocumentInput) -> str:
        body = document.model_dump(mode="json", exclude={"filename"})
        if body["source_uri"] is None:
            body.pop("source_uri")
        for assertion in body["assertions"]:
            if assertion["time_precision"] == "EXACT":
                assertion.pop("time_precision")
        # Extraction order is not evidence identity. Sorting prevents a
        # harmless parser reorder from bypassing the idempotency key.
        body["assertions"] = sorted(body["assertions"], key=canonical_json)
        return sha256_json(body)

    def ingest_document(self, case_id: str, document: DocumentInput) -> MutationResult:
        content_hash = self._document_hash(document)

        with self.database.session() as session, session.begin():
            # The case row is the mutation serialization boundary. The content
            # hash check must happen after this lock; checking first allows two
            # concurrent uploads to both observe absence and race the unique
            # constraint instead of returning the same logical result.
            case = session.scalar(
                select(CaseRecord).where(CaseRecord.id == case_id).with_for_update()
            )
            if case is None:
                raise KeyError(f"Unknown case: {case_id}")

            existing = session.scalar(
                select(DocumentRecord).where(
                    DocumentRecord.case_id == case_id,
                    DocumentRecord.content_hash == content_hash,
                )
            )
            if existing is not None:
                return MutationResult(
                    case_id=case_id,
                    document_id=existing.id,
                    change_set_id=None,
                    revision=case.revision,
                    operation="ADD_DOCUMENT",
                    deduplicated=True,
                    affected_keys=[],
                    queued_artifacts=0,
                    untouched_artifacts=self._artifact_count(session, case_id),
                )

            case.revision += 1
            revision = case.revision
            document_record = DocumentRecord(
                id=new_id(),
                case_id=case_id,
                filename=document.filename,
                source_type=document.source_type,
                source_uri=document.source_uri,
                content_hash=content_hash,
                added_at_revision=revision,
            )
            session.add(document_record)
            # Establish the source row before immutable assertions reference it.
            # Explicit ordering keeps the unit of work correct without ORM
            # relationships that are not otherwise needed by the kernel.
            session.flush()

            assertions: list[AssertionRecord] = []
            for item in document.assertions:
                record = AssertionRecord(
                    id=new_id(),
                    case_id=case_id,
                    document_id=document_record.id,
                    entity_id=item.entity_id,
                    occurred_at=item.occurred_at,
                    kind=item.kind,
                    value=item.value,
                    time_precision=item.time_precision,
                    source_locator=item.source_locator,
                    source_text=item.source_text,
                    added_at_revision=revision,
                )
                assertions.append(record)
                session.add(record)

            keys = sorted({timeline_key(item.entity_id, item.occurred_at) for item in assertions})
            artifacts = self._touch_keys_and_queue(session, case_id, revision, keys)
            session.flush()
            total = self._artifact_count(session, case_id)

            change_set = ChangeSetRecord(
                id=new_id(),
                case_id=case_id,
                revision=revision,
                operation="ADD_DOCUMENT",
                performed_by=case.assigned_officer,
                document_id=document_record.id,
                affected_keys=keys,
                queued_artifact_ids=[item.id for item in artifacts],
                untouched_artifacts=total - len(artifacts),
            )
            session.add(change_set)

            return MutationResult(
                case_id=case_id,
                document_id=document_record.id,
                change_set_id=change_set.id,
                revision=revision,
                operation="ADD_DOCUMENT",
                deduplicated=False,
                affected_keys=keys,
                queued_artifacts=len(artifacts),
                untouched_artifacts=total - len(artifacts),
            )

    def retract_document(self, case_id: str, document_id: str, reason: str) -> MutationResult:
        with self.database.session() as session, session.begin():
            case = session.scalar(
                select(CaseRecord).where(CaseRecord.id == case_id).with_for_update()
            )
            if case is None:
                raise KeyError(f"Unknown case: {case_id}")

            document = session.scalar(
                select(DocumentRecord).where(
                    DocumentRecord.id == document_id,
                    DocumentRecord.case_id == case_id,
                )
            )
            if document is None:
                raise KeyError(f"Unknown document: {document_id}")

            existing = session.scalar(
                select(DocumentRetractionRecord).where(
                    DocumentRetractionRecord.document_id == document_id
                )
            )
            if existing is not None:
                return MutationResult(
                    case_id=case_id,
                    document_id=document_id,
                    change_set_id=None,
                    revision=case.revision,
                    operation="RETRACT_DOCUMENT",
                    deduplicated=True,
                    affected_keys=[],
                    queued_artifacts=0,
                    untouched_artifacts=self._artifact_count(session, case_id),
                )

            case.revision += 1
            revision = case.revision
            session.add(
                DocumentRetractionRecord(
                    id=new_id(),
                    case_id=case_id,
                    document_id=document_id,
                    reason=reason,
                    retracted_at_revision=revision,
                )
            )

            assertions = session.scalars(
                select(AssertionRecord).where(AssertionRecord.document_id == document_id)
            ).all()
            keys = sorted({timeline_key(item.entity_id, item.occurred_at) for item in assertions})
            artifacts = self._touch_keys_and_queue(session, case_id, revision, keys)
            session.flush()
            total = self._artifact_count(session, case_id)

            change_set = ChangeSetRecord(
                id=new_id(),
                case_id=case_id,
                revision=revision,
                operation="RETRACT_DOCUMENT",
                performed_by=case.assigned_officer,
                document_id=document_id,
                affected_keys=keys,
                queued_artifact_ids=[item.id for item in artifacts],
                untouched_artifacts=total - len(artifacts),
            )
            session.add(change_set)

            return MutationResult(
                case_id=case_id,
                document_id=document_id,
                change_set_id=change_set.id,
                revision=revision,
                operation="RETRACT_DOCUMENT",
                deduplicated=False,
                affected_keys=keys,
                queued_artifacts=len(artifacts),
                untouched_artifacts=total - len(artifacts),
            )

    def _touch_keys_and_queue(
        self,
        session: Session,
        case_id: str,
        revision: int,
        keys: list[str],
    ) -> list[ArtifactRecord]:
        artifacts_by_id: dict[str, ArtifactRecord] = {}
        for key in keys:
            change_key = session.scalar(
                select(ChangeKeyRecord).where(
                    ChangeKeyRecord.case_id == case_id,
                    ChangeKeyRecord.key == key,
                )
            )
            if change_key is None:
                change_key = ChangeKeyRecord(
                    id=new_id(), case_id=case_id, key=key, version=revision
                )
                session.add(change_key)
            else:
                change_key.version = revision

            session.flush()
            dependent_artifacts = session.scalars(
                select(ArtifactRecord)
                .join(
                    ArtifactVersionRecord,
                    ArtifactVersionRecord.id == ArtifactRecord.current_version_id,
                )
                .join(
                    ArtifactDependencyRecord,
                    ArtifactDependencyRecord.artifact_version_id == ArtifactVersionRecord.id,
                )
                .where(
                    ArtifactRecord.case_id == case_id,
                    ArtifactDependencyRecord.change_key == key,
                )
            ).all()

            # A key has no dependency record before its first successful build.
            # Only that bootstrap case falls back to artifact identity.
            if not dependent_artifacts:
                bootstrap_artifact = session.scalar(
                    select(ArtifactRecord).where(
                        ArtifactRecord.case_id == case_id,
                        ArtifactRecord.artifact_type == "ENTITY_DAY_TIMELINE",
                        ArtifactRecord.artifact_key == key,
                    )
                )
                if bootstrap_artifact is None:
                    bootstrap_artifact = ArtifactRecord(
                        id=new_id(),
                        case_id=case_id,
                        artifact_type="ENTITY_DAY_TIMELINE",
                        artifact_key=key,
                    )
                    session.add(bootstrap_artifact)
                    session.flush()
                if bootstrap_artifact.current_version_id is None:
                    dependent_artifacts = [bootstrap_artifact]

            for artifact in dependent_artifacts:
                artifacts_by_id[artifact.id] = artifact

        artifacts = list(artifacts_by_id.values())
        for artifact in artifacts:
            queued_job = session.scalar(
                select(RecomputeJobRecord).where(
                    RecomputeJobRecord.artifact_id == artifact.id,
                    RecomputeJobRecord.target_revision == revision,
                )
            )
            if queued_job is None:
                session.add(
                    RecomputeJobRecord(
                        id=new_id(),
                        case_id=case_id,
                        artifact_id=artifact.id,
                        target_revision=revision,
                        status="QUEUED",
                    )
                )

        return artifacts

    @staticmethod
    def _artifact_count(session: Session, case_id: str) -> int:
        return int(
            session.scalar(
                select(func.count(ArtifactRecord.id)).where(ArtifactRecord.case_id == case_id)
            )
            or 0
        )

    @staticmethod
    def active_assertions_for_key(session: Session, case_id: str, key: str) -> list[AssertionView]:
        entity_id, day = parse_timeline_key(key)
        start = datetime.fromisoformat(f"{day}T00:00:00+00:00")
        end = datetime.fromisoformat(f"{day}T23:59:59.999999+00:00")

        retracted = exists().where(
            DocumentRetractionRecord.document_id == AssertionRecord.document_id
        )
        rows = session.scalars(
            select(AssertionRecord)
            .where(
                AssertionRecord.case_id == case_id,
                AssertionRecord.entity_id == entity_id,
                AssertionRecord.occurred_at >= start,
                AssertionRecord.occurred_at <= end,
                ~retracted,
            )
            .order_by(AssertionRecord.occurred_at, AssertionRecord.id)
        ).all()
        return [
            AssertionView(
                assertion_id=row.id,
                document_id=row.document_id,
                entity_id=row.entity_id,
                occurred_at=row.occurred_at,
                kind=row.kind,
                value=row.value,
                time_precision=row.time_precision,
                source_locator=row.source_locator,
                source_text=row.source_text,
            )
            for row in rows
        ]

    def full_rebuild_state(self, case_id: str) -> dict[str, dict]:
        with self.database.session() as session:
            artifacts = session.scalars(
                select(ArtifactRecord)
                .where(ArtifactRecord.case_id == case_id)
                .order_by(ArtifactRecord.artifact_key)
            ).all()
            return {
                artifact.artifact_key: build_timeline(
                    artifact.artifact_key,
                    self.active_assertions_for_key(session, case_id, artifact.artifact_key),
                )[0]
                for artifact in artifacts
            }

    def incremental_state(self, case_id: str) -> dict[str, dict]:
        with self.database.session() as session:
            rows = session.execute(
                select(ArtifactRecord.artifact_key, ArtifactVersionRecord.payload)
                .join(
                    ArtifactVersionRecord,
                    ArtifactVersionRecord.id == ArtifactRecord.current_version_id,
                )
                .where(ArtifactRecord.case_id == case_id)
                .order_by(ArtifactRecord.artifact_key)
            ).all()
            return {key: payload for key, payload in rows}

    def case_workspace(self, case_id: str) -> dict:
        """Return the durable case state needed by the interactive workspace."""

        with self.database.session() as session:
            case = session.get(CaseRecord, case_id)
            if case is None:
                raise KeyError(f"Unknown case: {case_id}")

            assertion_count = (
                select(func.count(AssertionRecord.id))
                .where(AssertionRecord.document_id == DocumentRecord.id)
                .correlate(DocumentRecord)
                .scalar_subquery()
            )
            retracted_at_revision = (
                select(DocumentRetractionRecord.retracted_at_revision)
                .where(DocumentRetractionRecord.document_id == DocumentRecord.id)
                .correlate(DocumentRecord)
                .scalar_subquery()
            )
            retraction_reason = (
                select(DocumentRetractionRecord.reason)
                .where(DocumentRetractionRecord.document_id == DocumentRecord.id)
                .correlate(DocumentRecord)
                .scalar_subquery()
            )
            documents = session.execute(
                select(
                    DocumentRecord.id,
                    DocumentRecord.filename,
                    DocumentRecord.source_type,
                    DocumentRecord.source_uri,
                    DocumentRecord.added_at_revision,
                    DocumentRecord.created_at,
                    assertion_count.label("assertion_count"),
                    retracted_at_revision.label("retracted_at_revision"),
                    retraction_reason.label("retraction_reason"),
                )
                .where(DocumentRecord.case_id == case_id)
                .order_by(DocumentRecord.added_at_revision.desc())
            ).all()
            artifact_keys = session.scalars(
                select(ArtifactRecord.artifact_key)
                .where(ArtifactRecord.case_id == case_id)
                .order_by(ArtifactRecord.artifact_key)
            ).all()
            case_summary = {
                "id": case.id,
                "name": case.name,
                "revision": case.revision,
                "assigned_officer": case.assigned_officer,
                "assigned_badge": case.assigned_badge,
                "assigned_unit": case.assigned_unit,
                "handoff_note": case.handoff_note,
                "created_at": case.created_at.isoformat(),
            }

        artifacts = []
        for key in artifact_keys:
            artifact = self.current_artifact(case_id, key)
            artifacts.append(
                artifact
                or {
                    "artifact_key": key,
                    "version": 0,
                    "computed_at_revision": 0,
                    "fresh": False,
                    "dependency_versions": [],
                    "payload": {"events": []},
                    "lineage": [],
                }
            )

        return {
            "case": case_summary,
            "documents": [
                {
                    "id": row.id,
                    "filename": row.filename,
                    "source_type": row.source_type,
                    "source_uri": row.source_uri,
                    "added_at_revision": row.added_at_revision,
                    "created_at": row.created_at.isoformat(),
                    "assertion_count": int(row.assertion_count or 0),
                    "retracted": row.retracted_at_revision is not None,
                    "retracted_at_revision": row.retracted_at_revision,
                    "retraction_reason": row.retraction_reason,
                }
                for row in documents
            ],
            "artifacts": artifacts,
        }

    def case_proof(self, case_id: str) -> dict:
        """Return live evidence for the invariants highlighted by the demo UI."""

        incremental = self.incremental_state(case_id)
        rebuilt = self.full_rebuild_state(case_id)

        with self.database.session() as session:
            case = session.get(CaseRecord, case_id)
            if case is None:
                raise KeyError(f"Unknown case: {case_id}")

            retracted = exists().where(
                DocumentRetractionRecord.document_id == AssertionRecord.document_id
            )
            total_assertions = int(
                session.scalar(
                    select(func.count(AssertionRecord.id)).where(
                        AssertionRecord.case_id == case_id
                    )
                )
                or 0
            )
            active_assertions = int(
                session.scalar(
                    select(func.count(AssertionRecord.id)).where(
                        AssertionRecord.case_id == case_id,
                        ~retracted,
                    )
                )
                or 0
            )
            retained_retracted_assertions = int(
                session.scalar(
                    select(func.count(AssertionRecord.id)).where(
                        AssertionRecord.case_id == case_id,
                        retracted,
                    )
                )
                or 0
            )
            artifact_count = self._artifact_count(session, case_id)
            current_artifacts = int(
                session.scalar(
                    select(func.count(ArtifactRecord.id)).where(
                        ArtifactRecord.case_id == case_id,
                        ArtifactRecord.current_version_id.is_not(None),
                    )
                )
                or 0
            )
            artifact_versions = int(
                session.scalar(
                    select(func.count(ArtifactVersionRecord.id))
                    .join(ArtifactRecord, ArtifactRecord.id == ArtifactVersionRecord.artifact_id)
                    .where(ArtifactRecord.case_id == case_id)
                )
                or 0
            )
            change_keys = int(
                session.scalar(
                    select(func.count(ChangeKeyRecord.id)).where(
                        ChangeKeyRecord.case_id == case_id
                    )
                )
                or 0
            )
            retractions = int(
                session.scalar(
                    select(func.count(DocumentRetractionRecord.id)).where(
                        DocumentRetractionRecord.case_id == case_id
                    )
                )
                or 0
            )
            job_counts = {
                status: count
                for status, count in session.execute(
                    select(RecomputeJobRecord.status, func.count(RecomputeJobRecord.id))
                    .where(RecomputeJobRecord.case_id == case_id)
                    .group_by(RecomputeJobRecord.status)
                )
            }

        return {
            "case_revision": case.revision,
            "equivalent_to_full_rebuild": incremental == rebuilt,
            "artifacts": {
                "total": artifact_count,
                "current": current_artifacts,
                "immutable_versions": artifact_versions,
                "change_keys": change_keys,
            },
            "evidence": {
                "assertions_total": total_assertions,
                "assertions_active": active_assertions,
                "retractions": retractions,
                "retracted_source_assertions_retained": retained_retracted_assertions,
            },
            "queue": {
                "jobs_total": sum(job_counts.values()),
                "by_status": job_counts,
                "settled": all(
                    status in {"SUCCEEDED", "SUPERSEDED", "FAILED_PERMANENT"}
                    for status in job_counts
                ),
            },
        }

    def case_findings(self, case_id: str) -> dict:
        """Derive cross-source review findings from the active assertion set.

        Findings are recomputed in full on every read. They are a pure function
        of active assertions, so they inherit full-rebuild semantics without
        incremental machinery; if they became expensive they would become
        artifacts with change keys exactly like timelines.
        """

        with self.database.session() as session:
            case = session.get(CaseRecord, case_id)
            if case is None:
                raise KeyError(f"Unknown case: {case_id}")

            retracted = exists().where(
                DocumentRetractionRecord.document_id == AssertionRecord.document_id
            )
            rows = session.execute(
                select(AssertionRecord, DocumentRecord)
                .join(DocumentRecord, DocumentRecord.id == AssertionRecord.document_id)
                .where(AssertionRecord.case_id == case_id, ~retracted)
                .order_by(AssertionRecord.occurred_at, AssertionRecord.id)
            ).all()
            events = [
                ActiveEvent(
                    assertion_id=assertion.id,
                    document_id=document.id,
                    document_filename=document.filename,
                    document_source_type=document.source_type,
                    entity_id=assertion.entity_id,
                    day=assertion.occurred_at.date().isoformat(),
                    kind=assertion.kind,
                    value=assertion.value,
                    time_precision=assertion.time_precision,
                    source_locator=assertion.source_locator,
                    source_text=assertion.source_text,
                )
                for assertion, document in rows
            ]
            revision = case.revision

        findings = derive_findings(events)
        findings["case_id"] = case_id
        findings["case_revision"] = revision
        return findings

    def case_changes(self, case_id: str, limit: int = 10) -> dict:
        """Explain recent evidence mutations from the durable revision ledger.

        The finding delta is derived at each revision boundary, rather than
        stored as presentation copy. This keeps the brief reproducible from
        immutable assertions, retractions, and recomputation jobs.
        """

        with self.database.session() as session:
            case = session.get(CaseRecord, case_id)
            if case is None:
                raise KeyError(f"Unknown case: {case_id}")

            changes = session.scalars(
                select(ChangeSetRecord)
                .where(ChangeSetRecord.case_id == case_id)
                .order_by(ChangeSetRecord.revision.desc())
                .limit(max(1, min(limit, 50)))
            ).all()
            documents = {
                item.id: item
                for item in session.scalars(
                    select(DocumentRecord).where(DocumentRecord.case_id == case_id)
                ).all()
            }
            retractions = {
                item.document_id: item
                for item in session.scalars(
                    select(DocumentRetractionRecord).where(
                        DocumentRetractionRecord.case_id == case_id
                    )
                ).all()
            }
            assertion_rows = session.execute(
                select(AssertionRecord, DocumentRecord)
                .join(DocumentRecord, DocumentRecord.id == AssertionRecord.document_id)
                .where(AssertionRecord.case_id == case_id)
                .order_by(AssertionRecord.occurred_at, AssertionRecord.id)
            ).all()
            jobs = session.scalars(
                select(RecomputeJobRecord).where(RecomputeJobRecord.case_id == case_id)
            ).all()

            retracted_at = {
                document_id: item.retracted_at_revision
                for document_id, item in retractions.items()
            }

            def findings_at(revision: int) -> dict:
                events = [
                    ActiveEvent(
                        assertion_id=assertion.id,
                        document_id=document.id,
                        document_filename=document.filename,
                        document_source_type=document.source_type,
                        entity_id=assertion.entity_id,
                        day=assertion.occurred_at.date().isoformat(),
                        kind=assertion.kind,
                        value=assertion.value,
                        time_precision=assertion.time_precision,
                        source_locator=assertion.source_locator,
                        source_text=assertion.source_text,
                    )
                    for assertion, document in assertion_rows
                    if assertion.added_at_revision <= revision
                    and retracted_at.get(document.id, revision + 1) > revision
                ]
                return derive_findings(events)

            def finding_keys(findings: dict, category: str) -> set[tuple]:
                if category == "contradictions":
                    return {
                        (
                            item["entity_id"],
                            item["date"],
                            *item.get("classes", []),
                        )
                        for item in findings[category]
                    }
                if category == "corroborations":
                    return {
                        (item["entity_id"], item["date"], item.get("event_class"))
                        for item in findings[category]
                    }
                return {
                    (item["entity_id"], item["date"])
                    for item in findings[category]
                }

            findings_by_revision = {
                revision: findings_at(revision)
                for revision in {
                    max(0, item.revision - offset)
                    for item in changes
                    for offset in (0, 1)
                }
            }
            jobs_by_change = {
                change.id: [
                    job
                    for job in jobs
                    if job.target_revision == change.revision
                    and job.artifact_id in set(change.queued_artifact_ids)
                ]
                for change in changes
            }

            items = []
            terminal_statuses = {"SUCCEEDED", "SUPERSEDED", "FAILED_PERMANENT"}
            clean_statuses = {"SUCCEEDED", "SUPERSEDED"}
            for change in changes:
                document = documents[change.document_id]
                retraction = retractions.get(change.document_id)
                before = findings_by_revision[max(0, change.revision - 1)]
                after = findings_by_revision[change.revision]
                delta = {}
                for category in ("contradictions", "corroborations", "single_source"):
                    before_keys = finding_keys(before, category)
                    after_keys = finding_keys(after, category)
                    delta[category] = {
                        "opened": len(after_keys - before_keys),
                        "cleared": len(before_keys - after_keys),
                    }

                change_jobs = jobs_by_change[change.id]
                status_counts = {
                    status: sum(job.status == status for job in change_jobs)
                    for status in sorted({job.status for job in change_jobs})
                }
                requested = len(change.queued_artifact_ids)
                settled = len(change_jobs) == requested and all(
                    job.status in terminal_statuses for job in change_jobs
                )
                items.append(
                    {
                        "id": change.id,
                        "revision": change.revision,
                        "operation": change.operation,
                        "performed_by": change.performed_by,
                        "created_at": change.created_at.isoformat(),
                        "document": {
                            "id": document.id,
                            "filename": document.filename,
                            "source_type": document.source_type,
                            "source_uri": document.source_uri,
                            "retraction_reason": (
                                retraction.reason
                                if retraction is not None
                                and retraction.retracted_at_revision == change.revision
                                else None
                            ),
                        },
                        "affected": {
                            "timeline_count": len(change.affected_keys),
                            "timelines": [
                                {"key": key, "entity_id": entity_id, "date": day}
                                for key in change.affected_keys
                                for entity_id, day in [parse_timeline_key(key)]
                            ],
                            "untouched_artifacts": change.untouched_artifacts,
                        },
                        "findings_delta": delta,
                        "recomputation": {
                            "requested": requested,
                            "by_status": status_counts,
                            "settled": settled,
                            "completed_cleanly": settled
                            and all(job.status in clean_statuses for job in change_jobs),
                        },
                    }
                )

        proof = self.case_proof(case_id)
        failed_jobs = proof["queue"]["by_status"].get("FAILED_PERMANENT", 0)
        return {
            "case_id": case_id,
            "case_revision": proof["case_revision"],
            "current_verification": {
                "verified": (
                    proof["equivalent_to_full_rebuild"]
                    and proof["queue"]["settled"]
                    and failed_jobs == 0
                ),
                "equivalent_to_full_rebuild": proof["equivalent_to_full_rebuild"],
                "queue_settled": proof["queue"]["settled"],
            },
            "changes": items,
        }

    def current_artifact(self, case_id: str, key: str) -> dict | None:
        with self.database.session() as session:
            row = session.execute(
                select(
                    ArtifactRecord.id,
                    ArtifactRecord.artifact_key,
                    ArtifactVersionRecord.id.label("artifact_version_id"),
                    ArtifactVersionRecord.version,
                    ArtifactVersionRecord.payload,
                    ArtifactVersionRecord.lineage,
                    ArtifactVersionRecord.computed_at_revision,
                )
                .join(
                    ArtifactVersionRecord,
                    ArtifactVersionRecord.id == ArtifactRecord.current_version_id,
                )
                .where(
                    ArtifactRecord.case_id == case_id,
                    ArtifactRecord.artifact_key == key,
                )
            ).one_or_none()
            if row is None:
                return None
            dependency_versions = session.execute(
                select(
                    ArtifactDependencyRecord.change_key,
                    ArtifactDependencyRecord.observed_version,
                    ChangeKeyRecord.version.label("current_version"),
                )
                .join(
                    ChangeKeyRecord,
                    and_(
                        ChangeKeyRecord.case_id == case_id,
                        ChangeKeyRecord.key == ArtifactDependencyRecord.change_key,
                    ),
                )
                .where(ArtifactDependencyRecord.artifact_version_id == row.artifact_version_id)
                .order_by(ArtifactDependencyRecord.change_key)
            ).all()
            fresh = bool(dependency_versions) and all(
                dependency.observed_version == dependency.current_version
                for dependency in dependency_versions
            )
            return {
                "artifact_id": row.id,
                "artifact_key": row.artifact_key,
                "version": row.version,
                "computed_at_revision": row.computed_at_revision,
                "fresh": fresh,
                "dependency_versions": [
                    {
                        "key": dependency.change_key,
                        "observed": dependency.observed_version,
                        "current": dependency.current_version,
                    }
                    for dependency in dependency_versions
                ],
                "payload": row.payload,
                "lineage": row.lineage,
            }
