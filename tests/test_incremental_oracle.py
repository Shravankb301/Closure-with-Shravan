from __future__ import annotations

import random

from evidence_delta.service import EvidenceService
from evidence_delta.worker import RecomputeWorker
from tests.helpers import document


def test_randomized_add_and_retract_matches_full_rebuild_after_every_mutation(
    service: EvidenceService,
    worker: RecomputeWorker,
) -> None:
    """The crown-jewel invariant, tested after 300 state transitions."""

    rng = random.Random(20260731)
    case = service.create_case("Randomized equivalence case")
    active_documents: list[str] = []
    sequence = 0

    # Establish 100 independently addressable entity-day artifacts.
    for entity_number in range(10):
        for day in range(1, 11):
            result = service.ingest_document(
                case.id,
                document(sequence, f"entity-{entity_number}", day),
            )
            active_documents.append(result.document_id)
            sequence += 1
    worker.run_until_idle()
    assert service.incremental_state(case.id) == service.full_rebuild_state(case.id)

    for mutation in range(300):
        should_retract = active_documents and rng.random() < 0.40
        if should_retract:
            index = rng.randrange(len(active_documents))
            document_id = active_documents.pop(index)
            service.retract_document(
                case.id,
                document_id,
                reason=f"randomized correction {mutation}",
            )
        else:
            entity_id = f"entity-{rng.randrange(10)}"
            day = rng.randrange(1, 11)
            result = service.ingest_document(
                case.id,
                document(sequence, entity_id, day, hour=rng.randrange(24)),
            )
            active_documents.append(result.document_id)
            sequence += 1

        worker.run_until_idle()
        assert service.incremental_state(case.id) == service.full_rebuild_state(case.id), (
            f"Incremental state diverged after mutation {mutation}"
        )
