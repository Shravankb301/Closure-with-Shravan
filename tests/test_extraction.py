from __future__ import annotations

from fastapi.testclient import TestClient

from evidence_delta.api import create_app
from evidence_delta.extraction import extract_assertions

EXCERPT = (
    "On 2013-04-19, Kadyrbayev removed the backpack from the dorm room. "
    "He later disposed of it in New Bedford. "
    "In April 2013 the laptop was concealed at the apartment."
)


def test_deterministic_extraction_quotes_source_verbatim(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = extract_assertions(EXCERPT, filename="tip.txt")

    assert result["mode"] == "deterministic"
    assert result["model"] is None
    assert result["proposals"], "expected at least one proposed assertion"
    for proposal in result["proposals"]:
        # The core provenance guarantee: every span is copied verbatim.
        assert proposal["source_text"] in EXCERPT
        assert proposal["provenance_verified"] is True
        assert proposal["time_precision"] in {
            "EXACT", "MINUTE", "HOUR", "DAY", "MONTH", "WINDOW", "UNKNOWN"
        }
    kinds = {proposal["kind"] for proposal in result["proposals"]}
    assert "REPORTED_DISPOSAL" in kinds
    assert "REPORTED_CONCEALMENT" in kinds


def test_unverified_model_span_is_flagged(monkeypatch) -> None:
    # A model that paraphrases instead of quoting must be surfaced, not trusted.
    monkeypatch.setattr("evidence_delta.extraction.model_available", lambda: True)

    def fake_extract(excerpt, source_hint):
        return [{
            "entity_id": "backpack",
            "occurred_at": "2013-04-19T00:00:00+00:00",
            "kind": "REPORTED_DISPOSAL",
            "value": "backpack disposed",
            "time_precision": "DAY",
            "source_text": "a paraphrase that never appeared in the source",
            "source_locator": "para 1",
            "confidence": "high",
            "rationale": "test",
        }]

    monkeypatch.setattr("evidence_delta.extraction._extract_with_model", fake_extract)
    result = extract_assertions(EXCERPT)

    assert result["mode"] == "assisted"
    assert result["proposals"][0]["provenance_verified"] is False


def test_extract_endpoint_returns_proposals(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    app = create_app("sqlite+pysqlite:///:memory:")
    with TestClient(app) as client:
        response = client.post("/extract", json={"text": EXCERPT})
        assert response.status_code == 200
        body = response.json()
        assert body["mode"] in {"assisted", "deterministic"}
        assert isinstance(body["proposals"], list) and body["proposals"]


def test_confirmed_proposal_round_trips_into_a_document(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    app = create_app("sqlite+pysqlite:///:memory:")
    with TestClient(app) as client:
        case = client.post("/cases", json={"name": "AI intake round-trip"})
        assert case.status_code == 201
        case_id = case.json()["id"]

        proposals = client.post("/extract", json={"text": EXCERPT}).json()["proposals"]
        dated = next(p for p in proposals if p["occurred_at"])
        assertion = {
            key: dated[key]
            for key in (
                "entity_id", "occurred_at", "kind", "value",
                "time_precision", "source_locator", "source_text",
            )
        }
        added = client.post(
            f"/cases/{case_id}/documents",
            json={
                "filename": "ai-intake.txt",
                "source_type": "user_confirmed_extraction",
                "assertions": [assertion],
            },
        )
        assert added.status_code == 202
        assert added.json()["operation"] == "ADD_DOCUMENT"


def test_extract_rejects_empty_text() -> None:
    app = create_app("sqlite+pysqlite:///:memory:")
    with TestClient(app) as client:
        assert client.post("/extract", json={"text": ""}).status_code == 422
