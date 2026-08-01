from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from evidence_delta.database import Database
from evidence_delta.domain import (
    AssertionView,
    build_timeline,
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
from evidence_delta.schemas import DocumentInput, MutationResult


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

    @staticmethod
    def _document_hash(document: DocumentInput) -> str:
        body = document.model_dump(mode="json", exclude={"filename"})
        return sha256_json(body)

    def ingest_document(self, case_id: str, document: DocumentInput) -> MutationResult:
        content_hash = self._document_hash(document)

        with self.database.session() as session, session.begin():
            existing = session.scalar(
                select(DocumentRecord).where(
                    DocumentRecord.case_id == case_id,
                    DocumentRecord.content_hash == content_hash,
                )
            )
            if existing is not None:
                case_revision = session.scalar(
                    select(CaseRecord.revision).where(CaseRecord.id == case_id)
                )
                if case_revision is None:
                    raise KeyError(f"Unknown case: {case_id}")
                return MutationResult(
                    case_id=case_id,
                    document_id=existing.id,
                    change_set_id=None,
                    revision=case_revision,
                    operation="ADD_DOCUMENT",
                    deduplicated=True,
                    affected_keys=[],
                    queued_artifacts=0,
                    untouched_artifacts=self._artifact_count(session, case_id),
                )

            case = session.scalar(
                select(CaseRecord).where(CaseRecord.id == case_id).with_for_update()
            )
            if case is None:
                raise KeyError(f"Unknown case: {case_id}")

            case.revision += 1
            revision = case.revision
            document_record = DocumentRecord(
                id=new_id(),
                case_id=case_id,
                filename=document.filename,
                source_type=document.source_type,
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
                case_revision = session.scalar(
                    select(CaseRecord.revision).where(CaseRecord.id == case_id)
                )
                return MutationResult(
                    case_id=case_id,
                    document_id=document_id,
                    change_set_id=None,
                    revision=int(case_revision or 0),
                    operation="RETRACT_DOCUMENT",
                    deduplicated=True,
                    affected_keys=[],
                    queued_artifacts=0,
                    untouched_artifacts=self._artifact_count(session, case_id),
                )

            case = session.scalar(
                select(CaseRecord).where(CaseRecord.id == case_id).with_for_update()
            )
            if case is None:
                raise KeyError(f"Unknown case: {case_id}")

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

    def current_artifact(self, case_id: str, key: str) -> dict | None:
        with self.database.session() as session:
            row = session.execute(
                select(
                    ArtifactRecord.id,
                    ArtifactRecord.artifact_key,
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
            return {
                "artifact_id": row.id,
                "artifact_key": row.artifact_key,
                "version": row.version,
                "computed_at_revision": row.computed_at_revision,
                "payload": row.payload,
                "lineage": row.lineage,
            }
