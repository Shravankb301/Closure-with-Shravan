from __future__ import annotations

from fastapi.testclient import TestClient

from evidence_delta.api import create_app


def test_case_search_returns_ranked_source_citations() -> None:
    app = create_app("sqlite+pysqlite:///:memory:")

    with TestClient(app) as client:
        created = client.post("/demo/real-case/boston-obstruction")
        case_id = created.json()["case_id"]

        response = client.get(
            f"/cases/{case_id}/search",
            params={"q": "laptop concealment"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["query"] == "laptop concealment"
        assert body["total"] >= 1
        assert body["source_count"] >= 1
        assert body["case_revision"] == 4
        assert all(result["source_text"] for result in body["results"])
        assert all(result["source_locator"] for result in body["results"])
        assert all(result["document"]["filename"] for result in body["results"])
        assert body["results"] == sorted(
            body["results"],
            key=lambda result: (
                -result["score"],
                result["occurred_at"],
                result["assertion_id"],
            ),
        )


def test_case_search_excludes_retracted_sources() -> None:
    app = create_app("sqlite+pysqlite:///:memory:")

    with TestClient(app) as client:
        created = client.post("/demo/real-case/boston-obstruction")
        case_id = created.json()["case_id"]
        workspace = client.get(f"/cases/{case_id}").json()
        sentencing = next(
            document
            for document in workspace["documents"]
            if document["filename"] == "2015-06-02-kadyrbayev-sentencing-record.html"
        )

        before = client.get(
            f"/cases/{case_id}/search",
            params={"q": "continued concealing"},
        ).json()
        assert before["total"] == 1

        retracted = client.post(
            f"/cases/{case_id}/documents/{sentencing['id']}/retractions",
            json={"reason": "Test active-only search behavior."},
        )
        assert retracted.status_code == 202

        after = client.get(
            f"/cases/{case_id}/search",
            params={"q": "continued concealing"},
        ).json()
        assert after["total"] == 0
        assert after["results"] == []


def test_case_search_rejects_an_empty_query() -> None:
    app = create_app("sqlite+pysqlite:///:memory:")

    with TestClient(app) as client:
        created = client.post("/demo/real-case/boston-obstruction")
        case_id = created.json()["case_id"]

        response = client.get(f"/cases/{case_id}/search", params={"q": "   "})

        assert response.status_code == 422
        assert response.json()["detail"] == "Search query cannot be empty"


def test_case_search_handles_natural_wording_and_inflection() -> None:
    app = create_app("sqlite+pysqlite:///:memory:")

    with TestClient(app) as client:
        created = client.post("/demo/real-case/boston-obstruction")
        case_id = created.json()["case_id"]

        response = client.get(
            f"/cases/{case_id}/search",
            params={"q": "what happened to the laptop concealing"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["search_mode"] == "lexical_stem_fuzzy_v2"
        assert body["query_terms"] == ["laptop", "concealing"]
        assert body["total"] >= 1
