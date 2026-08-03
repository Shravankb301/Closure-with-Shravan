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
                "source_uri": "https://example.gov/witness-17",
                "assertions": [
                    {
                        "entity_id": "john-carter",
                        "occurred_at": "2026-03-14T20:20:00Z",
                        "kind": "OBSERVED_AT",
                        "value": "Entered Northside Storage",
                        "time_precision": "DAY",
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
        assert pending_workspace.json()["case"]["assigned_officer"] is None
        assert pending_workspace.json()["documents"] == [
            {
                "id": added.json()["document_id"],
                "filename": "witness.json",
                "source_type": "structured_fixture",
                "source_uri": "https://example.gov/witness-17",
                "added_at_revision": 1,
                "created_at": pending_workspace.json()["documents"][0]["created_at"],
                "assertion_count": 1,
                "retracted": False,
                "retracted_at_revision": None,
                "retraction_reason": None,
            }
        ]
        assert pending_workspace.json()["artifacts"][0]["fresh"] is False

        pending_changes = client.get(f"/cases/{case_id}/changes").json()
        assert pending_changes["current_verification"]["verified"] is False
        assert pending_changes["changes"][0]["recomputation"] == {
            "requested": 1,
            "by_status": {"QUEUED": 1},
            "settled": False,
            "completed_cleanly": False,
        }
        pending_operations = client.get(f"/cases/{case_id}/operations")
        assert pending_operations.status_code == 200
        pending_trace = pending_operations.json()
        assert pending_trace["operational"] is False
        assert [item["status"] for item in pending_trace["stages"]] == [
            "complete",
            "complete",
            "complete",
            "pending",
            "pending",
            "pending",
        ]
        assert pending_trace["jobs"][0]["status"] == "QUEUED"
        assert pending_trace["jobs"][0]["publication"] is None

        assigned = client.put(
            f"/cases/{case_id}/assignment",
            json={
                "assigned_officer": "Officer Elena Ruiz",
                "assigned_badge": "B-417",
                "assigned_unit": "Evidence Review Unit",
                "handoff_note": "Verify the disposal window against carrier records.",
            },
        )
        assert assigned.status_code == 200
        assert assigned.json()["assigned_officer"] == "Officer Elena Ruiz"
        assigned_workspace = client.get(f"/cases/{case_id}").json()
        assert assigned_workspace["case"]["assigned_badge"] == "B-417"
        assert assigned_workspace["case"]["handoff_note"].startswith("Verify")

        drained = client.post("/workers/drain")
        assert drained.status_code == 200
        assert drained.json()["processed"] == 1

        changes = client.get(f"/cases/{case_id}/changes")
        assert changes.status_code == 200
        brief = changes.json()
        assert brief["current_verification"]["verified"] is True
        assert brief["changes"][0]["operation"] == "ADD_DOCUMENT"
        assert brief["changes"][0]["document"]["filename"] == "witness.json"
        assert brief["changes"][0]["affected"]["timeline_count"] == 1
        assert brief["changes"][0]["affected"]["timelines"] == [
            {
                "key": "timeline:john-carter:2026-03-14",
                "entity_id": "john-carter",
                "date": "2026-03-14",
            }
        ]
        assert brief["changes"][0]["findings_delta"]["single_source"] == {
            "opened": 1,
            "cleared": 0,
        }
        assert brief["changes"][0]["recomputation"] == {
            "requested": 1,
            "by_status": {"SUCCEEDED": 1},
            "settled": True,
            "completed_cleanly": True,
        }

        operations = client.get(f"/cases/{case_id}/operations")
        assert operations.status_code == 200
        trace = operations.json()
        assert trace["operational"] is True
        assert all(item["status"] == "complete" for item in trace["stages"])
        assert trace["selectivity"] == {
            "affected_artifacts": 1,
            "untouched_artifacts": 0,
            "artifacts_considered": 1,
            "recomputed_percent": 100.0,
        }
        assert trace["jobs"][0]["status"] == "SUCCEEDED"
        assert trace["jobs"][0]["publication"]["version"] == 1
        assert trace["jobs"][0]["publication"]["dependencies_matched"] is True
        assert trace["artifacts"][0]["immutable_versions"] == 1
        assert trace["artifacts"][0]["fresh"] is True

        artifact = client.get(f"/cases/{case_id}/artifacts/timeline:john-carter:2026-03-14")
        assert artifact.status_code == 200
        assert artifact.json()["fresh"] is True
        assert artifact.json()["payload"]["events"][0]["value"] == ("Entered Northside Storage")
        assert artifact.json()["payload"]["events"][0]["time_precision"] == "DAY"

        current_workspace = client.get(f"/cases/{case_id}").json()
        assert current_workspace["artifacts"][0]["fresh"] is True
        assert current_workspace["artifacts"][0]["lineage"][0]["source_locator"] == "paragraph:4"
        assert current_workspace["artifacts"][0]["lineage"][0]["time_precision"] == "DAY"

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

        retraction_brief = client.get(f"/cases/{case_id}/changes").json()
        latest = retraction_brief["changes"][0]
        assert latest["operation"] == "RETRACT_DOCUMENT"
        assert latest["performed_by"] == "Officer Elena Ruiz"
        assert latest["document"]["retraction_reason"] == "source corrected"
        assert latest["findings_delta"]["single_source"] == {
            "opened": 0,
            "cleared": 1,
        }

        missing = client.get("/cases/does-not-exist/changes")
        assert missing.status_code == 404
        missing_operations = client.get("/cases/does-not-exist/operations")
        assert missing_operations.status_code == 404

        invalid_reason = client.post(
            f"/cases/{case_id}/documents/{added.json()['document_id']}/retractions",
            json={"reason": "x" * 10_001},
        )
        assert invalid_reason.status_code == 422
