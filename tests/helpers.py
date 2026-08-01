from __future__ import annotations

from datetime import UTC, datetime

from evidence_delta.schemas import AssertionInput, DocumentInput


def document(
    sequence: int,
    entity_id: str,
    day: int,
    hour: int = 12,
    value: str | None = None,
) -> DocumentInput:
    occurred_at = datetime(2026, 3, day, hour, sequence % 60, tzinfo=UTC)
    statement = value or f"Event {sequence} for {entity_id}"
    return DocumentInput(
        filename=f"evidence_{sequence:04d}.json",
        assertions=[
            AssertionInput(
                entity_id=entity_id,
                occurred_at=occurred_at,
                kind="OBSERVED_AT",
                value=statement,
                source_locator=f"record:{sequence}",
                source_text=statement,
            )
        ],
    )
