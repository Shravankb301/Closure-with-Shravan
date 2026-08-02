from __future__ import annotations

from pathlib import Path
from time import monotonic, sleep

import pytest
from fastapi.testclient import TestClient

from evidence_delta.api import create_app
from evidence_delta.database import Database
from evidence_delta.runtime import WorkerLoop
from evidence_delta.service import EvidenceService
from evidence_delta.worker import RecomputeWorker
from tests.helpers import document


def test_provider_postgres_url_uses_psycopg_three() -> None:
    assert Database.normalize_url("postgresql://user:pass@db/internal") == (
        "postgresql+psycopg://user:pass@db/internal"
    )
    assert Database.normalize_url("postgres://user:pass@db/internal") == (
        "postgresql+psycopg://user:pass@db/internal"
    )


def test_demo_key_protects_stateful_routes(monkeypatch) -> None:
    monkeypatch.setenv("DEMO_API_KEY", "interview-only")
    app = create_app("sqlite+pysqlite:///:memory:")

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/").status_code == 200
        assert client.post("/cases", json={"name": "Unauthorized"}).status_code == 401
        authorized = client.post(
            "/cases",
            json={"name": "Authorized"},
            headers={"X-Demo-Key": "interview-only"},
        )
        assert authorized.status_code == 201


def test_hosted_mode_refuses_to_start_without_demo_key(monkeypatch) -> None:
    monkeypatch.delenv("DEMO_API_KEY", raising=False)
    monkeypatch.setenv("REQUIRE_DEMO_API_KEY", "true")
    with pytest.raises(RuntimeError, match="DEMO_API_KEY is required"):
        create_app("sqlite+pysqlite:///:memory:")


def test_dashboard_serves_the_selectivity_experiment() -> None:
    app = create_app("sqlite+pysqlite:///:memory:")
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "Run the live proof" in response.text
        assert "Bring your own evidence" in response.text
        assert 'id="quick-evidence-form"' in response.text
        assert 'id="evidence-file"' in response.text
        assert "function parseCsv" in response.text
        assert "Open existing case" in response.text
        assert "Evidence graph" in response.text
        assert 'id="evidence-graph"' in response.text
        assert 'id="graph-inspector"' in response.text
        assert "Withdrawn evidence stays visible" in response.text
        assert 'entry.querySelector(".journal-copy span")' in response.text
        assert 'content="http://testserver/og.png"' in response.text
        preview = client.get("/og.png")
        assert preview.status_code == 200
        assert preview.headers["content-type"] == "image/png"


def test_dashboard_scenario_runs_addition_and_retraction() -> None:
    app = create_app("sqlite+pysqlite:///:memory:")
    with TestClient(app) as client:
        created = client.post("/demo/scenario")
        assert created.status_code == 201
        scenario = created.json()
        assert scenario["queued_artifacts"] == 3
        assert scenario["untouched_artifacts"] == 97

        assert client.post("/workers/drain").status_code == 200
        artifact_path = f"/cases/{scenario['case_id']}/artifacts/{scenario['affected_keys'][0]}"
        added = client.get(artifact_path)
        assert added.status_code == 200
        assert added.json()["fresh"] is True
        assert len(added.json()["payload"]["events"]) == 2

        addition_proof = client.get(f"/cases/{scenario['case_id']}/proof")
        assert addition_proof.status_code == 200
        assert addition_proof.json()["equivalent_to_full_rebuild"] is True
        assert addition_proof.json()["artifacts"] == {
            "total": 100,
            "current": 100,
            "immutable_versions": 103,
            "change_keys": 100,
        }
        assert addition_proof.json()["evidence"] == {
            "assertions_total": 103,
            "assertions_active": 103,
            "retractions": 0,
            "retracted_source_assertions_retained": 0,
        }

        retracted = client.post(
            f"/cases/{scenario['case_id']}/documents/{scenario['document_id']}/retractions",
            json={"reason": "dashboard test"},
        )
        assert retracted.status_code == 202
        assert retracted.json()["queued_artifacts"] == 3
        assert retracted.json()["untouched_artifacts"] == 97
        client.post("/workers/drain")
        assert len(client.get(artifact_path).json()["payload"]["events"]) == 1

        retraction_proof = client.get(f"/cases/{scenario['case_id']}/proof").json()
        assert retraction_proof["equivalent_to_full_rebuild"] is True
        assert retraction_proof["artifacts"]["immutable_versions"] == 106
        assert retraction_proof["evidence"] == {
            "assertions_total": 103,
            "assertions_active": 100,
            "retractions": 1,
            "retracted_source_assertions_retained": 3,
        }


def test_worker_loop_processes_durable_queue(tmp_path: Path) -> None:
    database = Database(f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}")
    database.create_schema()
    service = EvidenceService(database)
    case = service.create_case("Runtime worker")
    mutation = service.ingest_document(case.id, document(1, "entity-1", 3))
    runtime = WorkerLoop(RecomputeWorker(database), poll_seconds=0.01)
    runtime.start()

    artifact = None
    try:
        deadline = monotonic() + 5
        while monotonic() < deadline:
            artifact = service.current_artifact(case.id, mutation.affected_keys[0])
            if artifact is not None and artifact["fresh"]:
                break
            sleep(0.01)
    finally:
        runtime.stop()

    assert artifact is not None
    assert artifact["fresh"] is True
