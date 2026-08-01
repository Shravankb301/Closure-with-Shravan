from __future__ import annotations

from fastapi.testclient import TestClient

from evidence_delta.api import create_app


def test_api_vertical_slice() -> None:
    app = create_app("sqlite+pysqlite:///:memory:")
    with TestClient(app) as client:
        created = client.post("/cases", json={"name": "API case"})
        assert created.status_code == 201
        case_id = created.json()["id"]

        added = client.post(
            f"/cases/{case_id}/documents",
            json={
                "filename": "witness.json",
                "source_type": "structured_fixture",
                "assertions": [
                    {
                        "entity_id": "john-carter",
                        "occurred_at": "2026-03-14T20:20:00Z",
                        "kind": "OBSERVED_AT",
                        "value": "Entered Northside Storage",
                        "source_locator": "paragraph:4",
                        "source_text": "I saw John enter Northside Storage.",
                    }
                ],
            },
        )
        assert added.status_code == 202
        assert added.json()["queued_artifacts"] == 1

        drained = client.post("/workers/drain")
        assert drained.status_code == 200
        assert drained.json()["processed"] == 1

        artifact = client.get(f"/cases/{case_id}/artifacts/timeline:john-carter:2026-03-14")
        assert artifact.status_code == 200
        assert artifact.json()["payload"]["events"][0]["value"] == ("Entered Northside Storage")
