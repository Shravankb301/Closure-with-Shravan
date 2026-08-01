from __future__ import annotations

from datetime import UTC, datetime

from evidence_delta.schemas import AssertionInput, DocumentInput
from evidence_delta.service import EvidenceService
from evidence_delta.worker import RecomputeWorker


def scenario_document(sequence: int, entity_number: int, day: int) -> DocumentInput:
    text = f"Synthetic observation {sequence} for entity-{entity_number}"
    return DocumentInput(
        filename=f"baseline_{sequence:03d}.json",
        assertions=[
            AssertionInput(
                entity_id=f"entity-{entity_number}",
                occurred_at=datetime(2026, 3, day, 12, sequence % 60, tzinfo=UTC),
                kind="OBSERVED_AT",
                value=text,
                source_locator=f"record:{sequence}",
                source_text=text,
            )
        ],
    )


def build_selectivity_scenario(service: EvidenceService, worker: RecomputeWorker) -> dict:
    """Create the exact 3-of-100 scenario used in the interview demo."""

    case = service.create_case("Synthetic Northside case")
    sequence = 0
    for entity_number in range(10):
        for day in range(1, 11):
            service.ingest_document(
                case.id,
                scenario_document(sequence, entity_number, day),
            )
            sequence += 1
    worker.run_until_idle()

    delta = DocumentInput(
        filename="new_witness_statement.json",
        assertions=[
            AssertionInput(
                entity_id=f"entity-{index}",
                occurred_at=datetime(2026, 3, index + 1, 20, 20, tzinfo=UTC),
                kind="OBSERVED_AT",
                value=f"New witness observation for entity-{index}",
                source_locator=f"paragraph:{index + 1}",
                source_text=f"New witness observation for entity-{index}",
            )
            for index in range(3)
        ],
    )
    result = service.ingest_document(case.id, delta)
    return {
        "case_id": case.id,
        "document_id": result.document_id,
        "revision": result.revision,
        "operation": result.operation,
        "affected_keys": result.affected_keys,
        "queued_artifacts": result.queued_artifacts,
        "untouched_artifacts": result.untouched_artifacts,
        "baseline_artifacts": 100,
    }
