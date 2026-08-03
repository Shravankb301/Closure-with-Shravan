from __future__ import annotations

import re
from datetime import UTC, datetime
from difflib import SequenceMatcher
from uuid import uuid4

from sqlalchemy import and_, exists, func, select
from sqlalchemy.orm import Session

from evidence_delta.analysis import ActiveEvent, derive_findings
from evidence_delta.artifact_vault import ArtifactVault
from evidence_delta.database import Database
from evidence_delta.domain import (
    AssertionView,
    build_timeline,
    canonical_json,
    parse_timeline_key,
    sha256_json,
    timeline_key,
)
from evidence_delta.errors import ResourceNotFoundError
from evidence_delta.evidence_graph import build_evidence_graph
from evidence_delta.job_status import (
    FAILED_PERMANENT,
    QUEUED,
    SUCCEEDED,
    is_clean_terminal,
    is_terminal,
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
    SourceAcquisitionAttemptRecord,
    SourceAcquisitionRecord,
    utc_now,
)
from evidence_delta.public_artifacts import ArtifactAcquisition
from evidence_delta.schemas import (
    AssertionInput,
    CaseAssignmentInput,
    DocumentInput,
    MutationResult,
)

ADD_DOCUMENT = "ADD_DOCUMENT"
RETRACT_DOCUMENT = "RETRACT_DOCUMENT"
SEARCH_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "did",
        "for",
        "from",
        "happen",
        "happened",
        "in",
        "is",
        "of",
        "on",
        "the",
        "to",
        "was",
        "what",
        "when",
        "where",
        "who",
        "with",
    }
)


def new_id() -> str:
    return str(uuid4())


def _search_stem(value: str) -> str:
    for suffix in ("ments", "ment", "ingly", "ing", "edly", "ed", "es", "s"):
        if value.endswith(suffix) and len(value) - len(suffix) >= 4:
            return value[: -len(suffix)]
    return value


def _search_term_matches(term: str, text: str) -> bool:
    if term in text:
        return True
    term_stem = _search_stem(term)
    for token in re.findall(r"[a-z0-9]+", text):
        token_stem = _search_stem(token)
        if token_stem == term_stem:
            return True
        if (
            min(len(token_stem), len(term_stem)) >= 5
            and SequenceMatcher(None, token_stem, term_stem).ratio() >= 0.84
        ):
            return True
    return False


def _active_event(assertion: AssertionRecord, document: DocumentRecord) -> ActiveEvent:
    return ActiveEvent(
        assertion_id=assertion.id,
        document_id=document.id,
        document_filename=document.filename,
        document_source_type=document.source_type,
        document_source_uri=document.source_uri,
        entity_id=assertion.entity_id,
        occurred_at=assertion.occurred_at.isoformat(),
        kind=assertion.kind,
        value=assertion.value,
        time_precision=assertion.time_precision,
        source_locator=assertion.source_locator,
        source_text=assertion.source_text,
    )


class EvidenceService:
    def __init__(self, database: Database, artifact_vault: ArtifactVault | None = None) -> None:
        self.database = database
        self.artifact_vault = artifact_vault or ArtifactVault()

    def create_case(self, name: str) -> CaseRecord:
        with self.database.session() as session, session.begin():
            record = CaseRecord(id=new_id(), name=name, revision=0)
            session.add(record)
        return record

    def assign_case(self, case_id: str, assignment: CaseAssignmentInput) -> dict:
        with self.database.session() as session, session.begin():
            case = self._locked_case(session, case_id)
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

    @staticmethod
    def _case(session: Session, case_id: str) -> CaseRecord:
        case = session.get(CaseRecord, case_id)
        if case is None:
            raise ResourceNotFoundError(f"Unknown case: {case_id}")
        return case

    @staticmethod
    def _locked_case(session: Session, case_id: str) -> CaseRecord:
        case = session.scalar(select(CaseRecord).where(CaseRecord.id == case_id).with_for_update())
        if case is None:
            raise ResourceNotFoundError(f"Unknown case: {case_id}")
        return case

    @staticmethod
    def _timeline_keys(assertions: list[AssertionRecord]) -> list[str]:
        return sorted({timeline_key(item.entity_id, item.occurred_at) for item in assertions})

    def _deduplicated_mutation(
        self,
        session: Session,
        case: CaseRecord,
        document_id: str,
        operation: str,
    ) -> MutationResult:
        return MutationResult(
            case_id=case.id,
            document_id=document_id,
            change_set_id=None,
            revision=case.revision,
            operation=operation,
            deduplicated=True,
            affected_keys=[],
            queued_artifacts=0,
            untouched_artifacts=self._artifact_count(session, case.id),
        )

    def _record_mutation(
        self,
        session: Session,
        case: CaseRecord,
        document_id: str,
        operation: str,
        affected_keys: list[str],
    ) -> MutationResult:
        artifacts = self._touch_keys_and_queue(
            session,
            case.id,
            case.revision,
            affected_keys,
        )
        session.flush()
        untouched_artifacts = self._artifact_count(session, case.id) - len(artifacts)
        change_set = ChangeSetRecord(
            id=new_id(),
            case_id=case.id,
            revision=case.revision,
            operation=operation,
            performed_by=case.assigned_officer,
            document_id=document_id,
            affected_keys=affected_keys,
            queued_artifact_ids=[item.id for item in artifacts],
            untouched_artifacts=untouched_artifacts,
        )
        session.add(change_set)
        return MutationResult(
            case_id=case.id,
            document_id=document_id,
            change_set_id=change_set.id,
            revision=case.revision,
            operation=operation,
            deduplicated=False,
            affected_keys=affected_keys,
            queued_artifacts=len(artifacts),
            untouched_artifacts=untouched_artifacts,
        )

    def ingest_document(self, case_id: str, document: DocumentInput) -> MutationResult:
        content_hash = self._document_hash(document)

        with self.database.session() as session, session.begin():
            # The case row is the mutation serialization boundary. The content
            # hash check must happen after this lock; checking first allows two
            # concurrent uploads to both observe absence and race the unique
            # constraint instead of returning the same logical result.
            case = self._locked_case(session, case_id)

            existing = session.scalar(
                select(DocumentRecord).where(
                    DocumentRecord.case_id == case_id,
                    DocumentRecord.content_hash == content_hash,
                )
            )
            if existing is not None:
                return self._deduplicated_mutation(
                    session,
                    case,
                    existing.id,
                    ADD_DOCUMENT,
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

            return self._record_mutation(
                session,
                case,
                document_record.id,
                ADD_DOCUMENT,
                self._timeline_keys(assertions),
            )

    def record_source_acquisition(
        self,
        case_id: str,
        document_id: str,
        acquisition: ArtifactAcquisition,
    ) -> None:
        """Attach retrieval metadata and its first chained custody event."""

        storage_status, storage_uri = self._store_artifact(acquisition)

        with self.database.session() as session, session.begin():
            document = session.scalar(
                select(DocumentRecord).where(
                    DocumentRecord.id == document_id,
                    DocumentRecord.case_id == case_id,
                )
            )
            if document is None:
                raise ResourceNotFoundError(f"Unknown document: {document_id}")
            existing = session.scalar(
                select(SourceAcquisitionRecord).where(
                    SourceAcquisitionRecord.document_id == document_id
                )
            )
            if existing is not None:
                return
            record = SourceAcquisitionRecord(
                id=new_id(),
                case_id=case_id,
                document_id=document_id,
                requested_uri=acquisition.requested_uri,
                resolved_uri=acquisition.resolved_uri,
                status=acquisition.status,
                http_status=acquisition.http_status,
                content_type=acquisition.content_type,
                content_bytes=acquisition.content_bytes,
                content_sha256=acquisition.content_sha256,
                extraction_method=acquisition.extraction_method,
                extracted_characters=acquisition.extracted_characters,
                page_count=acquisition.page_count,
                assertions_total=acquisition.assertions_total,
                assertions_verified=acquisition.assertions_verified,
                verification_status=acquisition.verification_status,
                error_class=acquisition.error_class,
                acquisition_method=acquisition.acquisition_method,
                storage_status=storage_status,
                storage_uri=storage_uri,
                attempt_count=1,
            )
            session.add(record)
            self._append_acquisition_attempt(
                session,
                case_id,
                document_id,
                acquisition,
                storage_uri,
                actor="system:public-connector",
            )

    def replace_source_acquisition(
        self,
        case_id: str,
        document_id: str,
        acquisition: ArtifactAcquisition,
        *,
        actor: str,
    ) -> None:
        """Promote a reviewer import while preserving prior attempts in custody history."""

        storage_status, storage_uri = self._store_artifact(acquisition)
        with self.database.session() as session, session.begin():
            self._case(session, case_id)
            record = session.scalar(
                select(SourceAcquisitionRecord).where(
                    SourceAcquisitionRecord.case_id == case_id,
                    SourceAcquisitionRecord.document_id == document_id,
                )
            )
            if record is None:
                raise ResourceNotFoundError(f"Unknown source acquisition: {document_id}")
            record.requested_uri = acquisition.requested_uri
            record.resolved_uri = acquisition.resolved_uri
            record.status = acquisition.status
            record.http_status = acquisition.http_status
            record.content_type = acquisition.content_type
            record.content_bytes = acquisition.content_bytes
            record.content_sha256 = acquisition.content_sha256
            record.extraction_method = acquisition.extraction_method
            record.extracted_characters = acquisition.extracted_characters
            record.page_count = acquisition.page_count
            record.assertions_total = acquisition.assertions_total
            record.assertions_verified = acquisition.assertions_verified
            record.verification_status = acquisition.verification_status
            record.error_class = acquisition.error_class
            record.acquisition_method = acquisition.acquisition_method
            record.storage_status = storage_status
            record.storage_uri = storage_uri
            record.attempt_count += 1
            record.retrieved_at = utc_now()
            self._append_acquisition_attempt(
                session,
                case_id,
                document_id,
                acquisition,
                storage_uri,
                actor=actor,
            )

    def document_input(self, case_id: str, document_id: str) -> DocumentInput:
        """Reconstruct the immutable source mapping for reprocessing."""

        with self.database.session() as session:
            self._case(session, case_id)
            document = session.scalar(
                select(DocumentRecord).where(
                    DocumentRecord.case_id == case_id,
                    DocumentRecord.id == document_id,
                )
            )
            if document is None:
                raise ResourceNotFoundError(f"Unknown document: {document_id}")
            assertions = session.scalars(
                select(AssertionRecord)
                .where(AssertionRecord.document_id == document_id)
                .order_by(AssertionRecord.created_at, AssertionRecord.id)
            ).all()
            return DocumentInput(
                filename=document.filename,
                source_type=document.source_type,
                source_uri=document.source_uri,
                assertions=[
                    AssertionInput(
                        entity_id=item.entity_id,
                        occurred_at=item.occurred_at
                        if item.occurred_at.tzinfo
                        else item.occurred_at.replace(tzinfo=UTC),
                        kind=item.kind,
                        value=item.value,
                        time_precision=item.time_precision,
                        source_locator=item.source_locator,
                        source_text=item.source_text,
                    )
                    for item in assertions
                ],
            )

    def _store_artifact(self, acquisition: ArtifactAcquisition) -> tuple[str, str | None]:
        try:
            receipt = self.artifact_vault.store(
                acquisition.content or b"", acquisition.content_sha256
            )
        except OSError:
            # Some function runtimes expose the deployed application as a
            # read-only filesystem. The acquisition metadata and fingerprint
            # remain useful, but the API must not claim that custody storage
            # succeeded or fail the entire case build.
            return "STORAGE_UNAVAILABLE", None
        if receipt is None:
            return "NOT_STORED", None
        return "STORED", receipt.uri

    @staticmethod
    def _acquisition_attempt_payload(
        *,
        case_id: str,
        document_id: str,
        sequence: int,
        acquisition_method: str,
        status: str,
        actor: str,
        content_sha256: str | None,
        content_bytes: int,
        storage_uri: str | None,
        previous_event_hash: str | None,
    ) -> dict:
        return {
            "case_id": case_id,
            "document_id": document_id,
            "sequence": sequence,
            "acquisition_method": acquisition_method,
            "status": status,
            "actor": actor,
            "content_sha256": content_sha256,
            "content_bytes": content_bytes,
            "storage_uri": storage_uri,
            "previous_event_hash": previous_event_hash,
        }

    def _append_acquisition_attempt(
        self,
        session: Session,
        case_id: str,
        document_id: str,
        acquisition: ArtifactAcquisition,
        storage_uri: str | None,
        *,
        actor: str,
    ) -> None:
        previous = session.scalar(
            select(SourceAcquisitionAttemptRecord)
            .where(SourceAcquisitionAttemptRecord.document_id == document_id)
            .order_by(SourceAcquisitionAttemptRecord.sequence.desc())
            .limit(1)
        )
        sequence = previous.sequence + 1 if previous else 1
        previous_hash = previous.event_hash if previous else None
        payload = self._acquisition_attempt_payload(
            case_id=case_id,
            document_id=document_id,
            sequence=sequence,
            acquisition_method=acquisition.acquisition_method,
            status=acquisition.status,
            actor=actor,
            content_sha256=acquisition.content_sha256,
            content_bytes=acquisition.content_bytes,
            storage_uri=storage_uri,
            previous_event_hash=previous_hash,
        )
        session.add(
            SourceAcquisitionAttemptRecord(
                id=new_id(),
                **payload,
                event_hash=sha256_json(payload),
            )
        )

    def case_source_acquisitions(self, case_id: str) -> dict:
        """Return the public-artifact acquisition audit for one case."""

        with self.database.session() as session:
            case = self._case(session, case_id)
            rows = session.execute(
                select(DocumentRecord, SourceAcquisitionRecord)
                .outerjoin(
                    SourceAcquisitionRecord,
                    SourceAcquisitionRecord.document_id == DocumentRecord.id,
                )
                .where(DocumentRecord.case_id == case_id)
                .order_by(DocumentRecord.added_at_revision, DocumentRecord.id)
            ).all()
            attempts = session.scalars(
                select(SourceAcquisitionAttemptRecord)
                .where(SourceAcquisitionAttemptRecord.case_id == case_id)
                .order_by(
                    SourceAcquisitionAttemptRecord.document_id,
                    SourceAcquisitionAttemptRecord.sequence,
                )
            ).all()

        attempts_by_document: dict[str, list[SourceAcquisitionAttemptRecord]] = {}
        for attempt in attempts:
            attempts_by_document.setdefault(attempt.document_id, []).append(attempt)

        items = []
        for document, acquisition in rows:
            item = {
                "document_id": document.id,
                "filename": document.filename,
                "source_type": document.source_type,
                "source_uri": document.source_uri,
                "assertions_organized": 0,
                "acquisition": None,
            }
            if acquisition is not None:
                source_attempts = attempts_by_document.get(document.id, [])
                previous_hash = None
                chain_verified = bool(source_attempts)
                for attempt in source_attempts:
                    payload = self._acquisition_attempt_payload(
                        case_id=attempt.case_id,
                        document_id=attempt.document_id,
                        sequence=attempt.sequence,
                        acquisition_method=attempt.acquisition_method,
                        status=attempt.status,
                        actor=attempt.actor,
                        content_sha256=attempt.content_sha256,
                        content_bytes=attempt.content_bytes,
                        storage_uri=attempt.storage_uri,
                        previous_event_hash=attempt.previous_event_hash,
                    )
                    if (
                        attempt.previous_event_hash != previous_hash
                        or sha256_json(payload) != attempt.event_hash
                    ):
                        chain_verified = False
                    previous_hash = attempt.event_hash
                artifact_verified = self.artifact_vault.verify(
                    acquisition.storage_uri,
                    acquisition.content_sha256,
                )
                item["assertions_organized"] = acquisition.assertions_total
                item["acquisition"] = {
                    "status": acquisition.status,
                    "requested_uri": acquisition.requested_uri,
                    "resolved_uri": acquisition.resolved_uri,
                    "http_status": acquisition.http_status,
                    "content_type": acquisition.content_type,
                    "content_bytes": acquisition.content_bytes,
                    "content_sha256": acquisition.content_sha256,
                    "extraction_method": acquisition.extraction_method,
                    "extracted_characters": acquisition.extracted_characters,
                    "page_count": acquisition.page_count,
                    "assertions_total": acquisition.assertions_total,
                    "assertions_verified": acquisition.assertions_verified,
                    "verification_status": acquisition.verification_status,
                    "error_class": acquisition.error_class,
                    "acquisition_method": acquisition.acquisition_method,
                    "storage_status": acquisition.storage_status,
                    "storage_uri": acquisition.storage_uri,
                    "attempt_count": acquisition.attempt_count,
                    "custody": {
                        "chain_status": "VERIFIED"
                        if chain_verified
                        else "FAILED"
                        if source_attempts
                        else "NOT_RECORDED",
                        "artifact_integrity": "VERIFIED" if artifact_verified else "NOT_VERIFIED",
                        "attempts": [
                            {
                                "sequence": attempt.sequence,
                                "method": attempt.acquisition_method,
                                "status": attempt.status,
                                "actor": attempt.actor,
                                "content_sha256": attempt.content_sha256,
                                "content_bytes": attempt.content_bytes,
                                "event_hash": attempt.event_hash,
                                "created_at": attempt.created_at.isoformat(),
                            }
                            for attempt in source_attempts
                        ],
                    },
                    "retrieved_at": acquisition.retrieved_at.isoformat(),
                }
            items.append(item)

        status_counts: dict[str, int] = {}
        for item in items:
            status = item["acquisition"]["status"] if item["acquisition"] else "NOT_REQUESTED"
            status_counts[status] = status_counts.get(status, 0) + 1
        return {
            "case_id": case.id,
            "case_revision": case.revision,
            "pipeline": ["FETCH", "FINGERPRINT", "READ", "VERIFY", "INGEST", "MAP"],
            "by_status": status_counts,
            "sources": items,
        }

    def retract_document(self, case_id: str, document_id: str, reason: str) -> MutationResult:
        with self.database.session() as session, session.begin():
            case = self._locked_case(session, case_id)

            document = session.scalar(
                select(DocumentRecord).where(
                    DocumentRecord.id == document_id,
                    DocumentRecord.case_id == case_id,
                )
            )
            if document is None:
                raise ResourceNotFoundError(f"Unknown document: {document_id}")

            existing = session.scalar(
                select(DocumentRetractionRecord).where(
                    DocumentRetractionRecord.document_id == document_id
                )
            )
            if existing is not None:
                return self._deduplicated_mutation(
                    session,
                    case,
                    document_id,
                    RETRACT_DOCUMENT,
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
            return self._record_mutation(
                session,
                case,
                document_id,
                RETRACT_DOCUMENT,
                self._timeline_keys(list(assertions)),
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
                        status=QUEUED,
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
            case = self._case(session, case_id)

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
            case = self._case(session, case_id)

            retracted = exists().where(
                DocumentRetractionRecord.document_id == AssertionRecord.document_id
            )
            total_assertions = int(
                session.scalar(
                    select(func.count(AssertionRecord.id)).where(AssertionRecord.case_id == case_id)
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
                    select(func.count(ChangeKeyRecord.id)).where(ChangeKeyRecord.case_id == case_id)
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
                "settled": all(is_terminal(status) for status in job_counts),
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
            case = self._case(session, case_id)
            events = self._active_events(session, case_id)
            revision = case.revision

        findings = derive_findings(events)
        findings["case_id"] = case_id
        findings["case_revision"] = revision
        return findings

    def case_evidence_graph(self, case_id: str) -> dict:
        """Return the live evidence-to-insight graph mapped from ledger rows."""

        with self.database.session() as session:
            case = self._case(session, case_id)
            events = self._active_events(session, case_id)
            findings = derive_findings(events)
            return build_evidence_graph(case.id, case.revision, events, findings)

    def case_search(self, case_id: str, query: str, limit: int = 12) -> dict:
        """Search active evidence and return ranked, source-cited assertions.

        This intentionally searches the governed evidence ledger instead of
        producing a generated answer. Every result can be inspected at its
        original source locator, and retracted documents never appear.
        """

        normalized_query = " ".join(query.lower().split())
        terms = [
            term
            for term in dict.fromkeys(re.findall(r"[a-z0-9]+", normalized_query))
            if term not in SEARCH_STOPWORDS
        ]
        if not terms:
            return {
                "case_id": case_id,
                "query": query,
                "query_terms": [],
                "search_mode": "lexical_stem_fuzzy_v2",
                "total": 0,
                "source_count": 0,
                "results": [],
            }

        with self.database.session() as session:
            case = self._case(session, case_id)
            events = self._active_events(session, case_id)
            revision = case.revision

        ranked = []
        for event in events:
            fields = {
                "entity": event.entity_id.replace("-", " ").lower(),
                "filename": event.document_filename.lower(),
                "source_type": event.document_source_type.replace("_", " ").lower(),
                "kind": event.kind.replace("_", " ").lower(),
                "value": event.value.lower(),
                "source_text": event.source_text.lower(),
                "source_locator": event.source_locator.lower(),
            }
            matched_terms = [
                term
                for term in terms
                if any(_search_term_matches(term, value) for value in fields.values())
            ]
            if len(matched_terms) != len(terms):
                continue

            score = 0
            combined = " ".join(fields.values())
            if normalized_query in combined:
                score += 12
            for term in terms:
                if _search_term_matches(term, fields["entity"]):
                    score += 6
                if _search_term_matches(term, fields["filename"]):
                    score += 5
                if _search_term_matches(term, fields["kind"]):
                    score += 4
                if _search_term_matches(term, fields["value"]):
                    score += 4
                if _search_term_matches(term, fields["source_text"]):
                    score += 2
                if _search_term_matches(term, fields["source_type"]) or _search_term_matches(
                    term, fields["source_locator"]
                ):
                    score += 1

            ranked.append(
                {
                    "assertion_id": event.assertion_id,
                    "entity_id": event.entity_id,
                    "occurred_at": event.occurred_at,
                    "kind": event.kind,
                    "value": event.value,
                    "time_precision": event.time_precision,
                    "source_locator": event.source_locator,
                    "source_text": event.source_text,
                    "document": {
                        "id": event.document_id,
                        "filename": event.document_filename,
                        "source_type": event.document_source_type,
                        "source_uri": event.document_source_uri,
                    },
                    "matched_terms": matched_terms,
                    "score": score,
                }
            )

        ranked.sort(
            key=lambda item: (
                -item["score"],
                item["occurred_at"],
                item["assertion_id"],
            )
        )
        bounded_limit = max(1, min(limit, 50))
        return {
            "case_id": case_id,
            "case_revision": revision,
            "query": query,
            "query_terms": terms,
            "search_mode": "lexical_stem_fuzzy_v2",
            "total": len(ranked),
            "source_count": len({item["document"]["id"] for item in ranked}),
            "results": ranked[:bounded_limit],
        }

    @staticmethod
    def _active_events(session: Session, case_id: str) -> list[ActiveEvent]:
        retracted = exists().where(
            DocumentRetractionRecord.document_id == AssertionRecord.document_id
        )
        rows = session.execute(
            select(AssertionRecord, DocumentRecord)
            .join(DocumentRecord, DocumentRecord.id == AssertionRecord.document_id)
            .where(AssertionRecord.case_id == case_id, ~retracted)
            .order_by(AssertionRecord.occurred_at, AssertionRecord.id)
        ).all()
        return [_active_event(assertion, document) for assertion, document in rows]

    def case_changes(self, case_id: str, limit: int = 10) -> dict:
        """Explain recent evidence mutations from the durable revision ledger.

        The finding delta is derived at each revision boundary, rather than
        stored as presentation copy. This keeps the brief reproducible from
        immutable assertions, retractions, and recomputation jobs.
        """

        with self.database.session() as session:
            self._case(session, case_id)

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
                document_id: item.retracted_at_revision for document_id, item in retractions.items()
            }

            def findings_at(revision: int) -> dict:
                events = [
                    _active_event(assertion, document)
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
                return {(item["entity_id"], item["date"]) for item in findings[category]}

            findings_by_revision = {
                revision: findings_at(revision)
                for revision in {
                    max(0, item.revision - offset) for item in changes for offset in (0, 1)
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
                    is_terminal(job.status) for job in change_jobs
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
                            and all(is_clean_terminal(job.status) for job in change_jobs),
                        },
                    }
                )

        proof = self.case_proof(case_id)
        failed_jobs = proof["queue"]["by_status"].get(FAILED_PERMANENT, 0)
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

    def case_operations(self, case_id: str, limit: int = 30) -> dict:
        """Expose the real mutation and recomputation pipeline for inspection.

        This response is intentionally assembled from durable database rows,
        rather than inferred from the current page state. It gives operators a
        compact way to verify which artifacts a revision touched, which jobs
        ran, what each job published, and whether the published dependencies
        still match the current change-key versions.
        """

        changes = self.case_changes(case_id, limit=1)
        with self.database.session() as session:
            case = self._case(session, case_id)

            job_rows = session.execute(
                select(RecomputeJobRecord, ArtifactRecord)
                .join(ArtifactRecord, ArtifactRecord.id == RecomputeJobRecord.artifact_id)
                .where(RecomputeJobRecord.case_id == case_id)
                .order_by(RecomputeJobRecord.created_at.desc(), RecomputeJobRecord.id.desc())
                .limit(max(1, min(limit, 100)))
            ).all()
            jobs = [row[0] for row in job_rows]
            artifacts_by_id = {row[1].id: row[1] for row in job_rows}
            job_ids = [job.id for job in jobs]
            published_versions = (
                session.scalars(
                    select(ArtifactVersionRecord).where(
                        ArtifactVersionRecord.source_job_id.in_(job_ids)
                    )
                ).all()
                if job_ids
                else []
            )
            versions_by_job = {version.source_job_id: version for version in published_versions}
            version_ids = [version.id for version in published_versions]
            dependencies = (
                session.scalars(
                    select(ArtifactDependencyRecord)
                    .where(ArtifactDependencyRecord.artifact_version_id.in_(version_ids))
                    .order_by(ArtifactDependencyRecord.change_key)
                ).all()
                if version_ids
                else []
            )
            dependencies_by_version: dict[str, list[ArtifactDependencyRecord]] = {}
            for dependency in dependencies:
                dependencies_by_version.setdefault(dependency.artifact_version_id, []).append(
                    dependency
                )

            change_versions = {
                key: version
                for key, version in session.execute(
                    select(ChangeKeyRecord.key, ChangeKeyRecord.version).where(
                        ChangeKeyRecord.case_id == case_id
                    )
                ).all()
            }
            artifact_records = session.scalars(
                select(ArtifactRecord)
                .where(ArtifactRecord.case_id == case_id)
                .order_by(ArtifactRecord.artifact_key)
            ).all()
            current_version_ids = [
                artifact.current_version_id
                for artifact in artifact_records
                if artifact.current_version_id is not None
            ]
            current_versions = {
                version.id: version
                for version in (
                    session.scalars(
                        select(ArtifactVersionRecord).where(
                            ArtifactVersionRecord.id.in_(current_version_ids)
                        )
                    ).all()
                    if current_version_ids
                    else []
                )
            }
            all_current_dependencies = (
                session.scalars(
                    select(ArtifactDependencyRecord).where(
                        ArtifactDependencyRecord.artifact_version_id.in_(current_version_ids)
                    )
                ).all()
                if current_version_ids
                else []
            )
            current_dependencies_by_version: dict[str, list[ArtifactDependencyRecord]] = {}
            for dependency in all_current_dependencies:
                current_dependencies_by_version.setdefault(
                    dependency.artifact_version_id, []
                ).append(dependency)
            version_counts = {
                artifact_id: int(count)
                for artifact_id, count in session.execute(
                    select(
                        ArtifactVersionRecord.artifact_id,
                        func.count(ArtifactVersionRecord.id),
                    )
                    .join(
                        ArtifactRecord,
                        ArtifactRecord.id == ArtifactVersionRecord.artifact_id,
                    )
                    .where(ArtifactRecord.case_id == case_id)
                    .group_by(ArtifactVersionRecord.artifact_id)
                ).all()
            }
            job_counts = {
                status: int(count)
                for status, count in session.execute(
                    select(
                        RecomputeJobRecord.status,
                        func.count(RecomputeJobRecord.id),
                    )
                    .where(RecomputeJobRecord.case_id == case_id)
                    .group_by(RecomputeJobRecord.status)
                ).all()
            }

        job_items = []
        for job in jobs:
            artifact = artifacts_by_id[job.artifact_id]
            published = versions_by_job.get(job.id)
            observed = (
                dependencies_by_version.get(published.id, []) if published is not None else []
            )
            dependency_items = [
                {
                    "change_key": dependency.change_key,
                    "observed_version": dependency.observed_version,
                    "current_version": change_versions.get(dependency.change_key),
                    "matched": dependency.observed_version
                    == change_versions.get(dependency.change_key),
                }
                for dependency in observed
            ]
            job_items.append(
                {
                    "id": job.id,
                    "artifact_id": artifact.id,
                    "artifact_key": artifact.artifact_key,
                    "target_revision": job.target_revision,
                    "status": job.status,
                    "attempts": job.attempts,
                    "failure_code": job.last_error,
                    "created_at": job.created_at.isoformat(),
                    "publication": (
                        {
                            "artifact_version_id": published.id,
                            "version": published.version,
                            "computed_at_revision": published.computed_at_revision,
                            "input_fingerprint": published.input_fingerprint,
                            "dependencies": dependency_items,
                            "dependencies_matched": bool(dependency_items)
                            and all(item["matched"] for item in dependency_items),
                        }
                        if published is not None
                        else None
                    ),
                }
            )

        artifact_items = []
        for artifact in artifact_records:
            current = current_versions.get(artifact.current_version_id or "")
            observed = (
                current_dependencies_by_version.get(current.id, []) if current is not None else []
            )
            dependencies_matched = bool(observed) and all(
                dependency.observed_version == change_versions.get(dependency.change_key)
                for dependency in observed
            )
            artifact_items.append(
                {
                    "id": artifact.id,
                    "artifact_key": artifact.artifact_key,
                    "current_version": current.version if current is not None else None,
                    "computed_at_revision": (
                        current.computed_at_revision if current is not None else None
                    ),
                    "immutable_versions": version_counts.get(artifact.id, 0),
                    "fresh": dependencies_matched,
                    "input_fingerprint": (
                        current.input_fingerprint if current is not None else None
                    ),
                    "dependency_count": len(observed),
                }
            )

        latest_change = changes["changes"][0] if changes["changes"] else None
        latest_recomputation = latest_change["recomputation"] if latest_change else None
        latest_requested = latest_recomputation["requested"] if latest_recomputation else 0
        latest_status_counts = latest_recomputation["by_status"] if latest_recomputation else {}
        latest_job_count = sum(latest_status_counts.values())
        latest_has_failure = latest_status_counts.get(FAILED_PERMANENT, 0) > 0
        latest_published = (
            latest_change is not None and latest_change["recomputation"]["completed_cleanly"]
        )
        all_artifacts_fresh = bool(artifact_items) and all(item["fresh"] for item in artifact_items)
        latest_dependencies_match = latest_published and all_artifacts_fresh
        equivalent_to_full_rebuild = changes["current_verification"]["equivalent_to_full_rebuild"]
        queue = {
            "jobs_total": sum(job_counts.values()),
            "by_status": job_counts,
            "settled": all(is_terminal(status) for status in job_counts),
        }

        def stage(stage_id: str, label: str, status: str, evidence: str) -> dict:
            return {
                "id": stage_id,
                "label": label,
                "status": status,
                "evidence": evidence,
            }

        stages = [
            stage(
                "commit",
                "Mutation committed",
                "complete" if latest_change else "pending",
                (
                    f"Revision {latest_change['revision']} recorded in the change ledger"
                    if latest_change
                    else "No evidence mutation has been recorded"
                ),
            ),
            stage(
                "invalidate",
                "Change keys advanced",
                "complete" if latest_change else "pending",
                (
                    f"{latest_change['affected']['timeline_count']} affected keys, "
                    f"{latest_change['affected']['untouched_artifacts']} artifacts untouched"
                    if latest_change
                    else "Waiting for the first mutation"
                ),
            ),
            stage(
                "queue",
                "Recompute jobs queued",
                (
                    "failed"
                    if latest_has_failure
                    else "complete"
                    if latest_change and latest_job_count == latest_requested
                    else "pending"
                ),
                (
                    f"{latest_job_count} durable jobs target revision {case.revision}"
                    if latest_change
                    else "Waiting for affected artifacts"
                ),
            ),
            stage(
                "publish",
                "Artifact versions published",
                "failed" if latest_has_failure else "complete" if latest_published else "pending",
                (
                    f"{latest_status_counts.get(SUCCEEDED, 0)} immutable versions published"
                    if latest_change
                    else "Waiting for worker publication"
                ),
            ),
            stage(
                "dependencies",
                "Dependencies verified",
                (
                    "failed"
                    if latest_has_failure
                    else "complete"
                    if latest_dependencies_match
                    else "pending"
                ),
                (
                    "Every published input version still matches its current change key"
                    if latest_dependencies_match
                    else "Verification completes after publication"
                ),
            ),
            stage(
                "oracle",
                "Full rebuild matched",
                "complete" if equivalent_to_full_rebuild else "pending",
                (
                    "Incremental state equals a deterministic rebuild from active assertions"
                    if equivalent_to_full_rebuild
                    else "Incremental state is not yet equivalent to a full rebuild"
                ),
            ),
        ]

        affected = latest_change["affected"]["timeline_count"] if latest_change else 0
        untouched = latest_change["affected"]["untouched_artifacts"] if latest_change else 0
        considered = affected + untouched
        return {
            "case_id": case_id,
            "case_revision": case.revision,
            "operational": (
                changes["current_verification"]["verified"]
                and all_artifacts_fresh
                and not latest_has_failure
            ),
            "stages": stages,
            "selectivity": {
                "affected_artifacts": affected,
                "untouched_artifacts": untouched,
                "artifacts_considered": considered,
                "recomputed_percent": round(affected / considered * 100, 1) if considered else 0.0,
            },
            "queue": queue,
            "artifacts": artifact_items,
            "jobs": job_items,
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
