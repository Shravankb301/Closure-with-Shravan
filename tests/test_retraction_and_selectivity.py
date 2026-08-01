from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select

from evidence_delta.models import AssertionRecord, DocumentRetractionRecord
from evidence_delta.schemas import AssertionInput, DocumentInput
from evidence_delta.service import EvidenceService
from evidence_delta.worker import RecomputeWorker
from tests.helpers import document


def test_retraction_is_append_only_and_recomputes_empty_timeline(
    service: EvidenceService,
    worker: RecomputeWorker,
) -> None:
    case = service.create_case("Retraction case")
    added = service.ingest_document(case.id, document(1, "entity-1", 4))
    worker.run_until_idle()

    result = service.retract_document(case.id, added.document_id, "source withdrawn")
    worker.run_until_idle()

    assert result.affected_keys == ["timeline:entity-1:2026-03-04"]
    artifact = service.current_artifact(case.id, result.affected_keys[0])
    assert artifact is not None
    assert artifact["payload"]["events"] == []

    with service.database.session() as session:
        assert session.scalar(select(func.count(AssertionRecord.id))) == 1
        assert session.scalar(select(func.count(DocumentRetractionRecord.id))) == 1


def test_one_document_recomputes_three_of_one_hundred_artifacts(
    service: EvidenceService,
    worker: RecomputeWorker,
) -> None:
    case = service.create_case("Selectivity case")
    sequence = 0
    for entity_number in range(10):
        for day in range(1, 11):
            service.ingest_document(
                case.id,
                document(sequence, f"entity-{entity_number}", day),
            )
            sequence += 1
    worker.run_until_idle()

    result = service.ingest_document(
        case.id,
        DocumentInput(
            filename="new_witness_statement.json",
            assertions=[
                AssertionInput(
                    entity_id=f"entity-{index}",
                    occurred_at=datetime(2026, 3, index + 1, 20, tzinfo=UTC),
                    kind="OBSERVED_AT",
                    value=f"New statement affecting entity {index}",
                    source_locator=f"paragraph:{index + 1}",
                    source_text=f"New statement affecting entity {index}",
                )
                for index in range(3)
            ],
        ),
    )

    assert result.queued_artifacts == 3
    assert result.untouched_artifacts == 97
    processed = worker.run_until_idle()
    assert len(processed) == 3
    assert service.incremental_state(case.id) == service.full_rebuild_state(case.id)
