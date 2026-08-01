from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AssertionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: str = Field(min_length=1, max_length=120)
    occurred_at: datetime
    kind: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1)
    source_locator: str = Field(min_length=1, max_length=160)
    source_text: str = Field(min_length=1)

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
    assertions: list[AssertionInput] = Field(min_length=1)


class CaseInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class RetractionInput(BaseModel):
    reason: str = Field(min_length=1)


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
