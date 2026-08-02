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

        pending_workspace = client.get(f"/cases/{case_id}")
        assert pending_workspace.status_code == 200
        assert pending_workspace.json()["case"]["name"] == "API case"
        assert pending_workspace.json()["documents"] == [
            {
                "id": added.json()["document_id"],
                "filename": "witness.json",
                "source_type": "structured_fixture",
                "added_at_revision": 1,
                "created_at": pending_workspace.json()["documents"][0]["created_at"],
                "assertion_count": 1,
                "retracted": False,
                "retracted_at_revision": None,
                "retraction_reason": None,
            }
        ]
        assert pending_workspace.json()["artifacts"][0]["fresh"] is False

        drained = client.post("/workers/drain")
        assert drained.status_code == 200
        assert drained.json()["processed"] == 1

        artifact = client.get(f"/cases/{case_id}/artifacts/timeline:john-carter:2026-03-14")
        assert artifact.status_code == 200
        assert artifact.json()["fresh"] is True
        assert artifact.json()["payload"]["events"][0]["value"] == ("Entered Northside Storage")

        current_workspace = client.get(f"/cases/{case_id}").json()
        assert current_workspace["artifacts"][0]["fresh"] is True
        assert current_workspace["artifacts"][0]["lineage"][0]["source_locator"] == "paragraph:4"

        retracted = client.post(
            f"/cases/{case_id}/documents/{added.json()['document_id']}/retractions",
            json={"reason": "source corrected"},
        )
        assert retracted.status_code == 202
        client.post("/workers/drain")
        retracted_workspace = client.get(f"/cases/{case_id}").json()
        assert retracted_workspace["documents"][0]["retracted"] is True
        assert retracted_workspace["documents"][0]["retraction_reason"] == "source corrected"
        assert retracted_workspace["artifacts"][0]["payload"]["events"] == []
