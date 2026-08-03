from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from evidence_delta.api import create_app
from evidence_delta.ocr import TesseractOCR
from evidence_delta.public_artifacts import ArtifactAcquisition, PublicArtifactClient
from evidence_delta.schemas import AssertionInput, DocumentInput


def public_document(
    source_text: str = "The source says the backpack was recovered.",
) -> DocumentInput:
    return DocumentInput(
        filename="official-record.html",
        source_type="agency_record",
        source_uri="https://www.justice.gov/official-record",
        assertions=[
            AssertionInput(
                entity_id="backpack",
                occurred_at="2026-08-03T12:00:00Z",
                kind="REPORTED_RECOVERY",
                value="The backpack was recovered.",
                source_locator="paragraph:2",
                source_text=source_text,
            )
        ],
    )


def test_html_artifact_is_fetched_read_and_verified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"].startswith("EvidenceDelta/")
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            text="<html><body><p>The source says the backpack was recovered.</p></body></html>",
        )

    client = PublicArtifactClient(transport=httpx.MockTransport(handler))
    result = client.acquire(public_document())

    assert result.status == "FETCHED"
    assert result.extraction_method == "HTML_TEXT"
    assert result.extracted_characters > 0
    assert result.assertions_verified == 1
    assert result.verification_status == "VERIFIED"
    assert len(result.content_sha256 or "") == 64


def test_access_challenge_is_reported_instead_of_treated_as_evidence() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            403,
            headers={"Content-Type": "text/html"},
            text=(
                "<html><title>Just a moment...</title>"
                "Enable JavaScript and cookies to continue</html>"
            ),
        )
    )
    result = PublicArtifactClient(transport=transport).acquire(public_document())

    assert result.status == "ACCESS_CHALLENGE"
    assert result.extraction_method == "BLOCKED_HTML"
    assert result.verification_status == "NOT_RUN"


def test_scanned_pdf_is_fingerprinted_and_marked_for_ocr() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    output = BytesIO()
    writer.write(output)
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            headers={"Content-Type": "application/pdf"},
            content=output.getvalue(),
        )
    )
    result = PublicArtifactClient(transport=transport).acquire(public_document())

    assert result.status == "FETCHED"
    assert result.page_count == 1
    assert result.extraction_method == "PDF_SCANNED_REQUIRES_OCR"
    assert result.extracted_characters == 0


def test_acquire_all_records_unexpected_runtime_failure(monkeypatch) -> None:
    client = PublicArtifactClient()
    monkeypatch.setattr(
        client,
        "acquire",
        lambda _document: (_ for _ in ()).throw(RuntimeError("runtime unavailable")),
    )

    result = client.acquire_all([public_document()])[0]

    assert result.status == "FETCH_FAILED"
    assert result.extraction_method == "NONE"
    assert result.error_class == "RuntimeError"


class FakePublicArtifactClient:
    def acquire_all(self, documents: list[DocumentInput]) -> list[ArtifactAcquisition]:
        return [
            ArtifactAcquisition(
                requested_uri=document.source_uri or "",
                resolved_uri=document.source_uri,
                status="FETCHED",
                http_status=200,
                content_type="text/html",
                content_bytes=2048,
                content_sha256=f"{index:064x}",
                extraction_method="HTML_TEXT",
                extracted_characters=1024,
                page_count=None,
                assertions_total=len(document.assertions),
                assertions_verified=len(document.assertions),
                verification_status="VERIFIED",
            )
            for index, document in enumerate(documents, start=1)
        ]

    def inspect_import(
        self,
        document: DocumentInput,
        content: bytes,
        *,
        content_type: str,
        resolved_uri: str | None = None,
    ) -> ArtifactAcquisition:
        return PublicArtifactClient().inspect_import(
            document,
            content,
            content_type=content_type,
            resolved_uri=resolved_uri,
        )


def test_demo_acquisition_is_persisted_and_exposed() -> None:
    app = create_app(
        "sqlite+pysqlite:///:memory:",
        artifact_client=FakePublicArtifactClient(),
    )
    with TestClient(app) as client:
        created = client.post("/demo/real-case/boston-obstruction?acquire_public_sources=true")
        assert created.status_code == 201
        case_id = created.json()["case_id"]
        assert created.json()["source_acquisition"]["by_status"] == {"FETCHED": 4}

        response = client.get(f"/cases/{case_id}/source-acquisitions")
        assert response.status_code == 200
        body = response.json()
        assert body["pipeline"] == ["FETCH", "FINGERPRINT", "READ", "VERIFY", "INGEST", "MAP"]
        assert len(body["sources"]) == 4
        assert sum(item["assertions_organized"] for item in body["sources"]) == 25
        assert all(
            item["acquisition"]["verification_status"] == "VERIFIED" for item in body["sources"]
        )


class FakeOCR:
    name = "FAKE_OCR"
    available = True

    def extract_pdf(self, _content: bytes) -> str:
        return "The source says the backpack was recovered."


def test_tesseract_adapter_renders_pages_and_caches(monkeypatch) -> None:
    calls = []

    monkeypatch.setattr("evidence_delta.ocr.shutil.which", lambda _name: "/usr/bin/tool")

    def fake_run(command, **_kwargs):
        calls.append(command)
        if command[0] == "pdftoppm":
            Path(f"{command[-1]}-1.jpg").write_bytes(b"rendered")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="Recovered source text", stderr="")

    monkeypatch.setattr("evidence_delta.ocr.subprocess.run", fake_run)
    adapter = TesseractOCR()

    assert adapter.available is True
    assert "Recovered source text" in adapter.extract_pdf(b"%PDF fixture")
    assert "Recovered source text" in adapter.extract_pdf(b"%PDF fixture")
    assert [command[0] for command in calls] == ["pdftoppm", "tesseract"]


def test_scanned_pdf_uses_configured_ocr_and_verifies_source_span() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    output = BytesIO()
    writer.write(output)
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            headers={"Content-Type": "application/pdf"},
            content=output.getvalue(),
        )
    )

    result = PublicArtifactClient(
        transport=transport,
        ocr_adapter=FakeOCR(),
    ).acquire(public_document())

    assert result.extraction_method == "FAKE_OCR"
    assert result.extracted_characters > 0
    assert result.assertions_verified == 1
    assert result.verification_status == "VERIFIED"


def test_reviewer_import_is_stored_and_appended_to_custody_chain(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ARTIFACT_VAULT_DIR", str(tmp_path / "vault"))
    app = create_app(
        "sqlite+pysqlite:///:memory:",
        artifact_client=FakePublicArtifactClient(),
    )

    with TestClient(app) as client:
        created = client.post("/demo/real-case/boston-obstruction?acquire_public_sources=true")
        case_id = created.json()["case_id"]
        report = client.get(f"/cases/{case_id}/source-acquisitions").json()
        source = report["sources"][0]
        document = app.state.service.document_input(case_id, source["document_id"])
        imported_html = (
            "<html><body>"
            + "\n".join(assertion.source_text for assertion in document.assertions)
            + "</body></html>"
        )

        response = client.post(
            f"/cases/{case_id}/source-acquisitions/{source['document_id']}/imports",
            json={
                "content_base64": base64.b64encode(imported_html.encode()).decode(),
                "content_type": "text/html",
                "resolved_uri": source["source_uri"],
            },
        )

        assert response.status_code == 200
        acquisition = response.json()["acquisition"]
        assert acquisition["acquisition_method"] == "REVIEWER_IMPORT"
        assert acquisition["storage_status"] == "STORED"
        assert acquisition["attempt_count"] == 2
        assert acquisition["assertions_verified"] == acquisition["assertions_total"]
        assert acquisition["custody"]["chain_status"] == "VERIFIED"
        assert acquisition["custody"]["artifact_integrity"] == "VERIFIED"
        assert len(acquisition["custody"]["attempts"]) == 2
        assert client.get(f"/cases/{case_id}").json()["case"]["revision"] == 4


def test_unavailable_artifact_storage_does_not_abort_case_build(tmp_path, monkeypatch) -> None:
    blocked_root = tmp_path / "blocked-vault"
    monkeypatch.setenv("ARTIFACT_VAULT_DIR", str(blocked_root))
    monkeypatch.setattr(
        "evidence_delta.artifact_vault.ArtifactVault.store",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("read only")),
    )
    app = create_app(
        "sqlite+pysqlite:///:memory:",
        artifact_client=FakePublicArtifactClient(),
    )

    with TestClient(app) as client:
        created = client.post("/demo/real-case/boston-obstruction?acquire_public_sources=true")

        assert created.status_code == 201
        case_id = created.json()["case_id"]
        report = client.get(f"/cases/{case_id}/source-acquisitions").json()
        acquisition = report["sources"][0]["acquisition"]
        assert acquisition["storage_status"] == "STORAGE_UNAVAILABLE"
        assert acquisition["storage_uri"] is None
        assert acquisition["custody"]["chain_status"] == "VERIFIED"
        assert acquisition["custody"]["artifact_integrity"] == "NOT_VERIFIED"
