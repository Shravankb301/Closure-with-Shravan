from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class CaseRecord(Base):
    __tablename__ = "cases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    assigned_officer: Mapped[str | None] = mapped_column(String(160), nullable=True)
    assigned_badge: Mapped[str | None] = mapped_column(String(80), nullable=True)
    assigned_unit: Mapped[str | None] = mapped_column(String(160), nullable=True)
    handoff_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class DocumentRecord(Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("case_id", "content_hash", name="uq_document_case_hash"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    case_id: Mapped[str] = mapped_column(
        ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[str] = mapped_column(String(80), nullable=False)
    source_uri: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    added_at_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class AssertionRecord(Base):
    """An immutable statement extracted from one source span.

    Retraction is represented by a separate DocumentRetractionRecord. The
    assertion row is never updated or deleted.
    """

    __tablename__ = "assertions"
    __table_args__ = (Index("ix_assertion_timeline", "case_id", "entity_id", "occurred_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    case_id: Mapped[str] = mapped_column(
        ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entity_id: Mapped[str] = mapped_column(String(120), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    time_precision: Mapped[str] = mapped_column(
        String(16), nullable=False, default="EXACT", server_default="EXACT"
    )
    source_locator: Mapped[str] = mapped_column(String(160), nullable=False)
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    added_at_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class DocumentRetractionRecord(Base):
    __tablename__ = "document_retractions"
    __table_args__ = (UniqueConstraint("document_id", name="uq_document_retraction"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    case_id: Mapped[str] = mapped_column(
        ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    retracted_at_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class ChangeKeyRecord(Base):
    __tablename__ = "change_keys"
    __table_args__ = (UniqueConstraint("case_id", "key", name="uq_change_key_case_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    case_id: Mapped[str] = mapped_column(
        ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(280), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class ArtifactRecord(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint("case_id", "artifact_type", "artifact_key", name="uq_artifact_identity"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    case_id: Mapped[str] = mapped_column(
        ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    artifact_type: Mapped[str] = mapped_column(String(80), nullable=False)
    artifact_key: Mapped[str] = mapped_column(String(280), nullable=False)
    current_version_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "artifact_versions.id",
            name="fk_artifact_current_version",
            use_alter=True,
            ondelete="SET NULL",
        ),
        nullable=True,
    )


class ArtifactVersionRecord(Base):
    __tablename__ = "artifact_versions"
    __table_args__ = (
        UniqueConstraint("artifact_id", "version", name="uq_artifact_version"),
        UniqueConstraint("source_job_id", name="uq_artifact_source_job"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_job_id: Mapped[str] = mapped_column(
        ForeignKey(
            "recompute_jobs.id",
            name="fk_artifact_version_source_job",
            use_alter=True,
        ),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    computed_at_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    lineage: Mapped[list] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class ArtifactDependencyRecord(Base):
    __tablename__ = "artifact_dependencies"
    __table_args__ = (
        UniqueConstraint("artifact_version_id", "change_key", name="uq_artifact_dependency"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    artifact_version_id: Mapped[str] = mapped_column(
        ForeignKey("artifact_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    change_key: Mapped[str] = mapped_column(String(280), nullable=False, index=True)
    observed_version: Mapped[int] = mapped_column(Integer, nullable=False)


class RecomputeJobRecord(Base):
    __tablename__ = "recompute_jobs"
    __table_args__ = (
        UniqueConstraint("artifact_id", "target_revision", name="uq_job_artifact_revision"),
        Index("ix_jobs_claim", "status", "lease_until", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    case_id: Mapped[str] = mapped_column(
        ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="QUEUED")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class ChangeSetRecord(Base):
    __tablename__ = "change_sets"
    __table_args__ = (UniqueConstraint("case_id", "revision", name="uq_change_set_revision"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    case_id: Mapped[str] = mapped_column(
        ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    operation: Mapped[str] = mapped_column(String(40), nullable=False)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    affected_keys: Mapped[list] = mapped_column(JSON, nullable=False)
    queued_artifact_ids: Mapped[list] = mapped_column(JSON, nullable=False)
    untouched_artifacts: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
