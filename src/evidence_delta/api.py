from __future__ import annotations

import base64
import binascii
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from evidence_delta.artifact_vault import ArtifactVault
from evidence_delta.database import Database
from evidence_delta.errors import ResourceNotFoundError
from evidence_delta.extraction import extract_assertions
from evidence_delta.ocr import default_ocr_adapter
from evidence_delta.public_artifacts import MAX_ARTIFACT_BYTES, PublicArtifactClient
from evidence_delta.real_case import build_boston_obstruction_case
from evidence_delta.runtime import WorkerLoop
from evidence_delta.scenario import build_selectivity_scenario
from evidence_delta.schemas import (
    ArtifactImportInput,
    CaseAssignmentInput,
    CaseInput,
    DocumentInput,
    ExtractionInput,
    RetractionInput,
)
from evidence_delta.service import EvidenceService
from evidence_delta.settings import AppSettings
from evidence_delta.worker import RecomputeWorker


@dataclass(frozen=True)
class AccessContext:
    actor: str
    role: str


def create_app(
    database_url: str | None = None,
    artifact_client: PublicArtifactClient | None = None,
) -> FastAPI:
    settings = AppSettings.from_environment(database_url)
    database = Database(settings.database_url)
    artifact_vault = ArtifactVault(settings.artifact_vault_dir)
    service = EvidenceService(database, artifact_vault)
    worker = RecomputeWorker(database)
    ocr_adapter = default_ocr_adapter() if settings.enable_local_ocr else None
    public_artifacts = artifact_client or PublicArtifactClient(ocr_adapter=ocr_adapter)
    configured_key = settings.demo_api_key
    read_only_key = settings.demo_read_only_key
    embedded_worker = settings.embedded_worker
    manual_drain = settings.manual_drain
    worker_runtime: WorkerLoop | None = None

    def access_context(supplied_key: str | None) -> AccessContext:
        if configured_key is None and read_only_key is None:
            return AccessContext(actor="local-demo-session", role="reviewer")
        supplied = supplied_key or ""
        if configured_key is not None and secrets.compare_digest(supplied, configured_key):
            return AccessContext(actor="authenticated-reviewer-key", role="reviewer")
        if read_only_key is not None and secrets.compare_digest(supplied, read_only_key):
            return AccessContext(actor="authenticated-viewer-key", role="viewer")
        raise HTTPException(status_code=401, detail="A valid access key is required")

    def require_viewer(
        x_demo_key: Annotated[str | None, Header()] = None,
    ) -> AccessContext:
        return access_context(x_demo_key)

    def require_reviewer(
        x_demo_key: Annotated[str | None, Header()] = None,
    ) -> AccessContext:
        access = access_context(x_demo_key)
        if access.role != "reviewer":
            raise HTTPException(status_code=403, detail="Reviewer access is required")
        return access

    read_secured = [Depends(require_viewer)]
    write_secured = [Depends(require_reviewer)]

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal worker_runtime
        # SQLite setup is safe and necessary for local use. Durable database
        # migrations run on startup only when the deployment profile permits it.
        # In particular, Vercel functions must not run Alembic during a request.
        if database.engine.dialect.name == "sqlite" or settings.migrate_on_startup:
            database.ensure_schema()
        if embedded_worker:
            worker_runtime = WorkerLoop(worker)
            worker_runtime.start()
        try:
            yield
        finally:
            if worker_runtime is not None:
                worker_runtime.stop()

    application = FastAPI(
        title="Evidence Delta Engine",
        version="0.1.0",
        lifespan=lifespan,
    )

    @application.exception_handler(ResourceNotFoundError)
    async def resource_not_found(
        _request: Request,
        error: ResourceNotFoundError,
    ) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @application.middleware("http")
    async def disable_response_caching(request: Request, call_next):
        # Case, proof, and findings responses must always reflect the current
        # evidence revision; a heuristically cached GET can show a reviewer a
        # retracted source as active. The social image is the only safe cache.
        response = await call_next(request)
        if request.url.path == "/og.png":
            response.headers.setdefault("Cache-Control", "public, max-age=3600")
        else:
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    static_dir = Path(__file__).parent / "static"

    @application.get("/", include_in_schema=False)
    def dashboard(request: Request) -> HTMLResponse:
        origin = escape(str(request.base_url).rstrip("/"), quote=True)
        html = (static_dir / "investigation.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("{{SITE_ORIGIN}}", origin))

    @application.get("/og.png", include_in_schema=False)
    def social_preview() -> FileResponse:
        return FileResponse(static_dir / "og.png", media_type="image/png")

    @application.get("/health")
    def health() -> dict:
        try:
            database.ping()
        except SQLAlchemyError as error:
            raise HTTPException(status_code=503, detail="Database unavailable") from error
        return {
            "status": "ok",
            "database": database.engine.dialect.name,
            "worker_mode": "embedded" if embedded_worker else "external_or_manual",
            "access_mode": "public_demo"
            if settings.public_demo_mode
            else "role_keys"
            if read_only_key
            else "reviewer_key"
            if configured_key
            else "local_open",
        }

    @application.post("/cases", status_code=201, dependencies=write_secured)
    def create_case(body: CaseInput) -> dict:
        record = service.create_case(body.name)
        return {"id": record.id, "name": record.name, "revision": record.revision}

    @application.get("/cases/{case_id}", dependencies=read_secured)
    def get_case_workspace(case_id: str) -> dict:
        return service.case_workspace(case_id)

    @application.put("/cases/{case_id}/assignment", dependencies=write_secured)
    def assign_case(case_id: str, body: CaseAssignmentInput) -> dict:
        return service.assign_case(case_id, body)

    @application.post("/demo/scenario", status_code=201, dependencies=write_secured)
    def create_demo_scenario() -> dict:
        return build_selectivity_scenario(service, worker)

    @application.post(
        "/demo/real-case/boston-obstruction",
        status_code=201,
        dependencies=write_secured,
    )
    def create_boston_obstruction_case(acquire_public_sources: bool = False) -> dict:
        client = public_artifacts if acquire_public_sources else None
        return build_boston_obstruction_case(service, worker, client)

    @application.post("/extract", dependencies=write_secured)
    def extract_evidence(body: ExtractionInput) -> dict:
        # AI proposes only. Nothing here touches the ledger; the reviewer
        # confirms and posts to /cases/{id}/documents to write immutable
        # evidence, keeping every stored assertion a human-authorized action.
        return extract_assertions(body.text, filename=body.filename, source_hint=body.source_hint)

    @application.post("/cases/{case_id}/documents", status_code=202, dependencies=write_secured)
    def add_document(case_id: str, body: DocumentInput) -> dict:
        return service.ingest_document(case_id, body).model_dump(mode="json")

    @application.post(
        "/cases/{case_id}/documents/{document_id}/retractions",
        status_code=202,
        dependencies=write_secured,
    )
    def retract_document(case_id: str, document_id: str, body: RetractionInput) -> dict:
        return service.retract_document(case_id, document_id, body.reason).model_dump(mode="json")

    @application.post("/workers/drain", dependencies=write_secured)
    def drain_worker() -> dict:
        if not manual_drain:
            raise HTTPException(status_code=404, detail="Manual worker drain is disabled")
        results = worker.run_until_idle()
        return {
            "processed": len(results),
            "artifacts": [item.model_dump(mode="json") for item in results],
        }

    @application.get("/cases/{case_id}/artifacts/{artifact_key:path}", dependencies=read_secured)
    def get_artifact(case_id: str, artifact_key: str) -> dict:
        result = service.current_artifact(case_id, artifact_key)
        if result is None:
            raise HTTPException(status_code=404, detail="Artifact not found")
        return result

    @application.get("/cases/{case_id}/findings", dependencies=read_secured)
    def get_case_findings(case_id: str) -> dict:
        return service.case_findings(case_id)

    @application.get("/cases/{case_id}/evidence-graph", dependencies=read_secured)
    def get_case_evidence_graph(case_id: str) -> dict:
        return service.case_evidence_graph(case_id)

    @application.get("/cases/{case_id}/search", dependencies=read_secured)
    def search_case_evidence(case_id: str, q: str, limit: int = 12) -> dict:
        if not q.strip():
            raise HTTPException(status_code=422, detail="Search query cannot be empty")
        if len(q) > 200:
            raise HTTPException(status_code=422, detail="Search query is limited to 200 characters")
        return service.case_search(case_id, q, limit)

    @application.get("/cases/{case_id}/source-acquisitions", dependencies=read_secured)
    def get_case_source_acquisitions(case_id: str) -> dict:
        return service.case_source_acquisitions(case_id)

    @application.post(
        "/cases/{case_id}/source-acquisitions/{document_id}/imports",
    )
    def import_source_artifact(
        case_id: str,
        document_id: str,
        body: ArtifactImportInput,
        x_demo_key: Annotated[str | None, Header()] = None,
    ) -> dict:
        access = require_reviewer(x_demo_key)
        try:
            content = base64.b64decode(body.content_base64, validate=True)
        except (ValueError, binascii.Error) as error:
            raise HTTPException(
                status_code=422, detail="Artifact content is not valid base64"
            ) from error
        if len(content) > MAX_ARTIFACT_BYTES:
            raise HTTPException(status_code=413, detail="Artifact exceeds the 8 MB limit")
        document = service.document_input(case_id, document_id)
        acquisition = public_artifacts.inspect_import(
            document,
            content,
            content_type=body.content_type,
            resolved_uri=body.resolved_uri,
        )
        service.replace_source_acquisition(
            case_id,
            document_id,
            acquisition,
            actor=access.actor,
        )
        report = service.case_source_acquisitions(case_id)
        return next(source for source in report["sources"] if source["document_id"] == document_id)

    @application.get("/cases/{case_id}/changes", dependencies=read_secured)
    def get_case_changes(case_id: str) -> dict:
        return service.case_changes(case_id)

    @application.get("/cases/{case_id}/operations", dependencies=read_secured)
    def get_case_operations(case_id: str) -> dict:
        return service.case_operations(case_id)

    @application.get("/cases/{case_id}/proof", dependencies=read_secured)
    def get_case_proof(case_id: str) -> dict:
        return service.case_proof(case_id)

    application.state.database = database
    application.state.service = service
    application.state.worker = worker
    application.state.artifact_vault = artifact_vault
    return application


app = create_app()
