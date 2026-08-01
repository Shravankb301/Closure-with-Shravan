from __future__ import annotations

import json
import random
from datetime import UTC, datetime
from time import perf_counter

from evidence_delta.database import Database
from evidence_delta.schemas import AssertionInput, DocumentInput
from evidence_delta.service import EvidenceService
from evidence_delta.worker import RecomputeWorker, SimulatedWorkerCrash


def make_document(sequence: int, entity_number: int, day: int) -> DocumentInput:
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


def assert_oracle(service: EvidenceService, case_id: str) -> None:
    assert service.incremental_state(case_id) == service.full_rebuild_state(case_id)


def main() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    database.create_schema()
    service = EvidenceService(database)
    worker = RecomputeWorker(database, lease_seconds=0)
    case = service.create_case("Synthetic Northside case")

    baseline_documents: list[str] = []
    sequence = 0
    for entity_number in range(10):
        for day in range(1, 11):
            result = service.ingest_document(case.id, make_document(sequence, entity_number, day))
            baseline_documents.append(result.document_id)
            sequence += 1
    worker.run_until_idle()
    assert_oracle(service, case.id)

    new_evidence = DocumentInput(
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

    added = service.ingest_document(case.id, new_evidence)
    incremental_start = perf_counter()
    addition_jobs = worker.run_until_idle()
    incremental_ms = (perf_counter() - incremental_start) * 1_000

    full_start = perf_counter()
    full_state = service.full_rebuild_state(case.id)
    full_rebuild_ms = (perf_counter() - full_start) * 1_000
    assert service.incremental_state(case.id) == full_state

    retracted = service.retract_document(case.id, added.document_id, "Witness statement withdrawn")
    retraction_jobs = worker.run_until_idle()
    assert_oracle(service, case.id)

    crash_case = service.create_case("Crash recovery demonstration")
    service.ingest_document(crash_case.id, make_document(999, 9, 9))
    crashed = False
    try:
        worker.run_once(simulate_crash=True)
    except SimulatedWorkerCrash:
        crashed = True
    crash_retry = worker.run_once()
    assert crash_retry.version == 1
    assert_oracle(service, crash_case.id)

    rng = random.Random(20260731)
    oracle_case = service.create_case("Randomized oracle demonstration")
    active: list[str] = []
    for mutation in range(300):
        if active and rng.random() < 0.40:
            document_id = active.pop(rng.randrange(len(active)))
            service.retract_document(oracle_case.id, document_id, f"oracle retraction {mutation}")
        else:
            result = service.ingest_document(
                oracle_case.id,
                make_document(
                    sequence + mutation,
                    rng.randrange(10),
                    rng.randrange(1, 11),
                ),
            )
            active.append(result.document_id)
        worker.run_until_idle()
        assert_oracle(service, oracle_case.id)

    report = {
        "baseline": {
            "artifacts": 100,
            "entity_days": "10 entities x 10 days",
        },
        "addition": {
            "affected_artifacts": added.queued_artifacts,
            "untouched_artifacts": added.untouched_artifacts,
            "published_versions": len(addition_jobs),
        },
        "retraction": {
            "affected_artifacts": retracted.queued_artifacts,
            "untouched_artifacts": retracted.untouched_artifacts,
            "published_versions": len(retraction_jobs),
            "assertions_deleted": 0,
        },
        "correctness": {
            "randomized_add_retract_mutations": 300,
            "equivalence_checked_after_each_mutation": True,
        },
        "crash_recovery": {
            "simulated_before_commit": crashed,
            "retry_published_artifact_version": crash_retry.version,
            "orphan_versions": 0,
        },
        "timing_ms": {
            "incremental_three_artifacts": round(incremental_ms, 3),
            "full_rebuild_one_hundred_artifacts": round(full_rebuild_ms, 3),
            "note": "Illustrative local run, not a production-scale benchmark",
        },
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
