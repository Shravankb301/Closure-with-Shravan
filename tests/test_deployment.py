from __future__ import annotations

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
        assert "Run 3-of-100 scenario" in response.text
        assert "Source lineage" in response.text


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

        retracted = client.post(
            f"/cases/{scenario['case_id']}/documents/{scenario['document_id']}/retractions",
            json={"reason": "dashboard test"},
        )
        assert retracted.status_code == 202
        assert retracted.json()["queued_artifacts"] == 3
        assert retracted.json()["untouched_artifacts"] == 97
        client.post("/workers/drain")
        assert len(client.get(artifact_path).json()["payload"]["events"]) == 1


def test_worker_loop_processes_durable_queue(database: Database) -> None:
    service = EvidenceService(database)
    case = service.create_case("Runtime worker")
    mutation = service.ingest_document(case.id, document(1, "entity-1", 3))
    runtime = WorkerLoop(RecomputeWorker(database), poll_seconds=0.01)
    runtime.start()

    deadline = monotonic() + 2
    artifact = None
    while monotonic() < deadline:
        artifact = service.current_artifact(case.id, mutation.affected_keys[0])
        if artifact is not None and artifact["fresh"]:
            break
        sleep(0.01)
    runtime.stop()

    assert artifact is not None
    assert artifact["fresh"] is True
