from __future__ import annotations

from fastapi.testclient import TestClient

from evidence_delta.api import create_app
from evidence_delta.real_case import boston_obstruction_documents
from tests.helpers import document


def test_graph_maps_live_ledger_rows_and_retraction(service, worker) -> None:
    case = service.create_case("dynamic evidence graph")
    mutation = service.ingest_document(case.id, document(1, "vehicle-one", 2))
    worker.run_until_idle()

    graph = service.case_evidence_graph(case.id)
    assert graph["case_revision"] == 1
    assert graph["mapping"] == {
        "engine": "deterministic-evidence-mapper-v1",
        "input": "active persistent assertions",
        "active_only": True,
        "relationship_types": [
            "CONTAINS",
            "DESCRIBES",
            "MAPS_TO",
            "PRODUCES_FINDING",
            "REVEALS_GAP",
        ],
    }
    assert graph["generated_from"] == {
        "documents": 1,
        "assertions": 1,
        "entities": 1,
        "findings": 1,
    }
    assertion = next(node for node in graph["nodes"] if node["type"] == "assertion")
    assert assertion["data"]["source_locator"] == "record:1"
    assert assertion["data"]["entity_id"] == "vehicle-one"
    assert {edge["relationship"] for edge in graph["edges"]} == {
        "CONTAINS",
        "DESCRIBES",
        "MAPS_TO",
        "PRODUCES_FINDING",
        "REVEALS_GAP",
    }
    assert graph == service.case_evidence_graph(case.id)

    service.retract_document(case.id, mutation.document_id, "Source withdrawn")
    worker.run_until_idle()
    retracted_graph = service.case_evidence_graph(case.id)
    assert retracted_graph["case_revision"] == 2
    assert retracted_graph["generated_from"] == {
        "documents": 0,
        "assertions": 0,
        "entities": 0,
        "findings": 0,
    }
    assert retracted_graph["nodes"] == []
    assert retracted_graph["edges"] == []


def test_graph_maps_findings_to_supporting_assertions(service, worker) -> None:
    case = service.create_case("public record graph")
    for source in boston_obstruction_documents():
        service.ingest_document(case.id, source)
    worker.run_until_idle()

    graph = service.case_evidence_graph(case.id)
    node_ids = {node["id"] for node in graph["nodes"]}
    assert graph["generated_from"]["documents"] == 4
    assert graph["generated_from"]["assertions"] == 25
    assert graph["generated_from"]["findings"] > 0
    assert {"SUPPORTS", "REVEALS_GAP"} <= set(graph["mapping"]["relationship_types"])
    assert all(edge["source"] in node_ids for edge in graph["edges"])
    assert all(edge["target"] in node_ids for edge in graph["edges"])
    assert any(
        node["type"] == "finding"
        and node["data"]["finding"]["reasoning"]["method"] == "deterministic_rule"
        for node in graph["nodes"]
    )


def test_evidence_graph_endpoint_is_live() -> None:
    app = create_app("sqlite+pysqlite:///:memory:")
    with TestClient(app) as client:
        created = client.post("/demo/real-case/boston-obstruction")
        case_id = created.json()["case_id"]

        response = client.get(f"/cases/{case_id}/evidence-graph")
        assert response.status_code == 200
        assert response.json()["case_id"] == case_id
        assert response.json()["generated_from"]["assertions"] == 25

        missing = client.get("/cases/does-not-exist/evidence-graph")
        assert missing.status_code == 404
