from __future__ import annotations

from pathlib import Path
from time import monotonic, sleep

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from evidence_delta.api import create_app
from evidence_delta.database import Database
from evidence_delta.runtime import WorkerLoop
from evidence_delta.service import EvidenceService
from evidence_delta.worker import RecomputeWorker
from tests.helpers import document


def test_api_responses_disable_http_caching() -> None:
    app = create_app("sqlite+pysqlite:///:memory:")
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.headers["Cache-Control"] == "no-store"
        created = client.post("/demo/real-case/boston-obstruction")
        case_id = created.json()["case_id"]
        for path in (f"/cases/{case_id}", f"/cases/{case_id}/findings", f"/cases/{case_id}/proof"):
            assert client.get(path).headers["Cache-Control"] == "no-store"
        assert client.get("/og.png").headers["Cache-Control"] == "public, max-age=3600"


def test_provider_postgres_url_uses_psycopg_three() -> None:
    assert Database.normalize_url("postgresql://user:pass@db/internal") == (
        "postgresql+psycopg://user:pass@db/internal"
    )
    assert Database.normalize_url("postgres://user:pass@db/internal") == (
        "postgresql+psycopg://user:pass@db/internal"
    )


def test_local_sqlite_schema_upgrades_legacy_evidence_columns(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'legacy.db'}")
    with database.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE cases ("
            "id VARCHAR(36) PRIMARY KEY, name VARCHAR(200) NOT NULL, "
            "revision INTEGER NOT NULL, created_at DATETIME NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE documents ("
            "id VARCHAR(36) PRIMARY KEY, case_id VARCHAR(36) NOT NULL, "
            "filename VARCHAR(255) NOT NULL, source_type VARCHAR(80) NOT NULL, "
            "content_hash VARCHAR(64) NOT NULL, added_at_revision INTEGER NOT NULL, "
            "created_at DATETIME NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE assertions ("
            "id VARCHAR(36) PRIMARY KEY, case_id VARCHAR(36) NOT NULL, "
            "document_id VARCHAR(36) NOT NULL, entity_id VARCHAR(120) NOT NULL, "
            "occurred_at DATETIME NOT NULL, kind VARCHAR(80) NOT NULL, value TEXT NOT NULL, "
            "source_locator VARCHAR(160) NOT NULL, source_text TEXT NOT NULL, "
            "added_at_revision INTEGER NOT NULL, created_at DATETIME NOT NULL)"
        )

    database.create_schema()

    schema = inspect(database.engine)
    assert "source_uri" in {column["name"] for column in schema.get_columns("documents")}
    assert "time_precision" in {
        column["name"] for column in schema.get_columns("assertions")
    }
    case_columns = {column["name"] for column in schema.get_columns("cases")}
    assert {"assigned_officer", "assigned_badge", "assigned_unit", "handoff_note"} <= (
        case_columns
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


def test_dashboard_serves_the_boston_evidence_command_board() -> None:
    app = create_app("sqlite+pysqlite:///:memory:")
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "Boston Evidence Command Board" in response.text
        assert "Reconstruct the case" in response.text
        assert "Open official-record case" in response.text
        assert "Evidence relationship graph" in response.text
        assert "Officer review queue" in response.text
        assert "Assign case ownership" in response.text
        assert 'id="evidence-form"' in response.text
        assert 'id="evidence-graph"' in response.text
        assert 'id="graph-inspector"' in response.text
        assert "function parseCsv" in response.text
        assert (
            'const REAL_CASE_TEMPLATE_ID = "boston-obstruction-public-record-v1"'
            in response.text
        )
        assert 'content="http://testserver/og.png"' in response.text

        preview = client.get("/og.png")
        assert preview.status_code == 200
        assert preview.headers["content-type"] == "image/png"


def test_official_record_case_is_materialized_with_status_separation() -> None:
    app = create_app("sqlite+pysqlite:///:memory:")
    with TestClient(app) as client:
        created = client.post("/demo/real-case/boston-obstruction")
        assert created.status_code == 201
        real_case = created.json()
        assert real_case["template_id"] == "boston-obstruction-public-record-v1"
        assert real_case["official_sources"] == 4
        assert real_case["assertions"] == 25
        assert real_case["court_established_assertions"] == 9
        assert real_case["materialized_timelines"] == 15
        assert real_case["equivalent_to_full_rebuild"] is True
        assert all(uri.startswith("https://www.") for uri in real_case["source_uris"])

        workspace = client.get(f"/cases/{real_case['case_id']}").json()
        assert len(workspace["documents"]) == 4
        assert all(document["source_uri"] for document in workspace["documents"])

        backpack = client.get(
            f"/cases/{real_case['case_id']}/artifacts/timeline:backpack:2013-04-19"
        ).json()
        kinds = {event["kind"] for event in backpack["payload"]["events"]}
        assert "COMPLAINT_OBJECT_DISPOSAL" in kinds
        assert "INDICTMENT_ALLEGED_DISPOSAL" in kinds
        assert "COURT_ESTABLISHED_DISPOSAL" in kinds
        assert all(event["time_precision"] == "WINDOW" for event in backpack["payload"]["events"])


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
