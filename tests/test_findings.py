from __future__ import annotations

from datetime import UTC, datetime

from evidence_delta.analysis import event_class, source_tier
from evidence_delta.real_case import boston_obstruction_documents
from evidence_delta.schemas import AssertionInput, DocumentInput
from evidence_delta.service import EvidenceService
from evidence_delta.worker import RecomputeWorker


def build_boston(service: EvidenceService, worker: RecomputeWorker) -> str:
    case = service.create_case("findings-boston")
    for document in boston_obstruction_documents():
        service.ingest_document(case.id, document)
    worker.run_until_idle()
    return case.id


def conflicting_tip() -> DocumentInput:
    return DocumentInput(
        filename="demo-conflicting-tip.txt",
        source_type="officer_observation",
        assertions=[
            AssertionInput(
                entity_id="laptop-computer",
                occurred_at=datetime(2013, 4, 19, 4, 30, tzinfo=UTC),
                kind="REPORTED_DISPOSAL",
                value=(
                    "DEMONSTRATION: hypothetical tip claiming the laptop was thrown "
                    "into the dumpster with the backpack."
                ),
                time_precision="WINDOW",
                source_locator="demo-tip:1",
                source_text="Caller stated the laptop went into the dumpster too.",
            )
        ],
    )


def test_kind_class_and_tier_mapping() -> None:
    assert event_class("COURT_ESTABLISHED_DISPOSAL") == "disposal"
    assert event_class("REPORTED_CONCEALMENT") == "concealment"
    assert event_class("TRANSFERRED") == "transfer"
    assert event_class("COURT_OUTCOME_SENTENCE") is None
    assert source_tier("court_outcome_record", "COURT_ESTABLISHED_DISPOSAL") == "court"
    assert source_tier("criminal_complaint", "COMPLAINT_OBJECT_DISPOSAL") == "allegation"
    assert source_tier("officer_observation", "REPORTED_DISPOSAL") == "officer"


def test_boston_record_is_internally_consistent(service, worker) -> None:
    case_id = build_boston(service, worker)
    findings = service.case_findings(case_id)

    assert findings["contradictions"] == []

    disposal = [
        finding
        for finding in findings["corroborations"]
        if finding["entity_id"] == "backpack"
        and finding["date"] == "2013-04-19"
        and finding["event_class"] == "disposal"
    ]
    assert len(disposal) == 1
    assert len(disposal[0]["documents"]) == 3
    assert disposal[0]["cross_tier"] is True
    assert set(disposal[0]["tiers"]) == {"allegation", "court"}

    single_source_days = {
        (finding["entity_id"], finding["date"]) for finding in findings["single_source"]
    }
    assert ("laptop-computer", "2013-04-19") in single_source_days
    assert ("backpack", "2013-04-26") in single_source_days


def test_conflicting_tip_surfaces_and_clears_contradiction(service, worker) -> None:
    case_id = build_boston(service, worker)

    mutation = service.ingest_document(case_id, conflicting_tip())
    worker.run_until_idle()

    findings = service.case_findings(case_id)
    assert len(findings["contradictions"]) == 1
    contradiction = findings["contradictions"][0]
    assert contradiction["entity_id"] == "laptop-computer"
    assert contradiction["date"] == "2013-04-19"
    assert contradiction["classes"] == ["concealment", "disposal"]
    assert len(contradiction["documents"]) == 2
    tiers = {event["tier"] for event in contradiction["events"]}
    assert tiers == {"court", "officer"}

    service.retract_document(case_id, mutation.document_id, "Tip withdrawn after review")
    worker.run_until_idle()

    cleared = service.case_findings(case_id)
    assert cleared["contradictions"] == []
    # Retraction removes the tip from reasoning but never deletes its record.
    proof = service.case_proof(case_id)
    assert proof["evidence"]["retracted_source_assertions_retained"] == 1
    assert proof["equivalent_to_full_rebuild"] is True


def test_findings_are_deterministic(service, worker) -> None:
    case_id = build_boston(service, worker)
    service.ingest_document(case_id, conflicting_tip())
    worker.run_until_idle()
    assert service.case_findings(case_id) == service.case_findings(case_id)


def test_findings_endpoint(service, worker, database) -> None:
    from fastapi.testclient import TestClient

    from evidence_delta.api import create_app

    app = create_app("sqlite+pysqlite:///:memory:")
    with TestClient(app) as client:
        created = client.post("/demo/real-case/boston-obstruction")
        assert created.status_code == 201
        case_id = created.json()["case_id"]

        response = client.get(f"/cases/{case_id}/findings")
        assert response.status_code == 200
        body = response.json()
        assert body["case_id"] == case_id
        assert body["contradictions"] == []
        assert body["corroborations"]
        assert body["generated_from"]["documents"] == 4

        missing = client.get("/cases/does-not-exist/findings")
        assert missing.status_code == 404
