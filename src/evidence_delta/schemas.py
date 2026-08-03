from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AssertionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: str = Field(min_length=1, max_length=120)
    occurred_at: datetime
    kind: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=20_000)
    time_precision: Literal["EXACT", "MINUTE", "HOUR", "DAY", "MONTH", "WINDOW", "UNKNOWN"] = (
        "EXACT"
    )
    source_locator: str = Field(min_length=1, max_length=160)
    source_text: str = Field(min_length=1, max_length=100_000)

    @field_validator("occurred_at")
    @classmethod
    def normalize_event_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("occurred_at must include a timezone")
        return value.astimezone(UTC)


class DocumentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1, max_length=255)
    source_type: str = Field(default="structured_fixture", min_length=1, max_length=80)
    source_uri: str | None = Field(default=None, max_length=2_048)
    assertions: list[AssertionInput] = Field(min_length=1, max_length=1_000)

    @field_validator("source_uri")
    @classmethod
    def validate_source_uri(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("source_uri must be an absolute HTTP(S) URL")
        return normalized


class CaseInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class CaseAssignmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assigned_officer: str | None = Field(default=None, max_length=160)
    assigned_badge: str | None = Field(default=None, max_length=80)
    assigned_unit: str | None = Field(default=None, max_length=160)
    handoff_note: str | None = Field(default=None, max_length=10_000)

    @field_validator("assigned_officer", "assigned_badge", "assigned_unit", "handoff_note")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class RetractionInput(BaseModel):
    reason: str = Field(min_length=1)


class ExtractionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=100_000)
    filename: str | None = Field(default=None, max_length=255)
    source_hint: str | None = Field(default=None, max_length=2_048)


class MutationResult(BaseModel):
    case_id: str
    document_id: str
    change_set_id: str | None
    revision: int
    operation: str
    deduplicated: bool
    affected_keys: list[str]
    queued_artifacts: int
    untouched_artifacts: int


class WorkerRunResult(BaseModel):
    claimed: bool
    published: bool = False
    job_id: str | None = None
    artifact_id: str | None = None
    artifact_key: str | None = None
    version: int | None = None
    reason: str | None = None
