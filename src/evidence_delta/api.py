from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.exc import SQLAlchemyError

from evidence_delta.database import Database
from evidence_delta.extraction import extract_assertions
from evidence_delta.real_case import build_boston_obstruction_case
from evidence_delta.runtime import WorkerLoop
from evidence_delta.scenario import build_selectivity_scenario
from evidence_delta.schemas import (
    CaseAssignmentInput,
    CaseInput,
    DocumentInput,
    ExtractionInput,
    RetractionInput,
)
from evidence_delta.service import EvidenceService
from evidence_delta.worker import RecomputeWorker


def create_app(database_url: str | None = None) -> FastAPI:
    url = database_url or os.getenv("DATABASE_URL", "sqlite:///./evidence_delta.db")
    database = Database(url)
    service = EvidenceService(database)
    worker = RecomputeWorker(database)
    configured_key = os.getenv("DEMO_API_KEY") or None
    demo_key_required = os.getenv("REQUIRE_DEMO_API_KEY", "false").lower() == "true"
    if demo_key_required and configured_key is None:
        raise RuntimeError("DEMO_API_KEY is required for this deployment")
    embedded_worker = os.getenv("RUN_EMBEDDED_WORKER", "false").lower() == "true"
    manual_drain = os.getenv("ENABLE_MANUAL_DRAIN", "true").lower() == "true"
    worker_runtime: WorkerLoop | None = None

    def require_demo_key(
        x_demo_key: Annotated[str | None, Header()] = None,
    ) -> None:
        if configured_key is None:
            return
        supplied = x_demo_key or ""
        if not secrets.compare_digest(supplied, configured_key):
            raise HTTPException(status_code=401, detail="A valid demo key is required")

    secured = [Depends(require_demo_key)]

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal worker_runtime
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
        }

    @application.post("/cases", status_code=201, dependencies=secured)
    def create_case(body: CaseInput) -> dict:
        record = service.create_case(body.name)
        return {"id": record.id, "name": record.name, "revision": record.revision}

    @application.get("/cases/{case_id}", dependencies=secured)
    def get_case_workspace(case_id: str) -> dict:
        try:
            return service.case_workspace(case_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @application.put("/cases/{case_id}/assignment", dependencies=secured)
    def assign_case(case_id: str, body: CaseAssignmentInput) -> dict:
        try:
            return service.assign_case(case_id, body)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @application.post("/demo/scenario", status_code=201, dependencies=secured)
    def create_demo_scenario() -> dict:
        return build_selectivity_scenario(service, worker)

    @application.post(
        "/demo/real-case/boston-obstruction",
        status_code=201,
        dependencies=secured,
    )
    def create_boston_obstruction_case() -> dict:
        return build_boston_obstruction_case(service, worker)

    @application.post("/extract", dependencies=secured)
    def extract_evidence(body: ExtractionInput) -> dict:
        # AI proposes only. Nothing here touches the ledger; the reviewer
        # confirms and posts to /cases/{id}/documents to write immutable
        # evidence, keeping every stored assertion a human-authorized action.
        return extract_assertions(
            body.text, filename=body.filename, source_hint=body.source_hint
        )

    @application.post("/cases/{case_id}/documents", status_code=202, dependencies=secured)
    def add_document(case_id: str, body: DocumentInput) -> dict:
        try:
            return service.ingest_document(case_id, body).model_dump(mode="json")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @application.post(
        "/cases/{case_id}/documents/{document_id}/retractions",
        status_code=202,
        dependencies=secured,
    )
    def retract_document(case_id: str, document_id: str, body: RetractionInput) -> dict:
        try:
            return service.retract_document(case_id, document_id, body.reason).model_dump(
                mode="json"
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @application.post("/workers/drain", dependencies=secured)
    def drain_worker() -> dict:
        if not manual_drain:
            raise HTTPException(status_code=404, detail="Manual worker drain is disabled")
        results = worker.run_until_idle()
        return {
            "processed": len(results),
            "artifacts": [item.model_dump(mode="json") for item in results],
        }

    @application.get("/cases/{case_id}/artifacts/{artifact_key:path}", dependencies=secured)
    def get_artifact(case_id: str, artifact_key: str) -> dict:
        result = service.current_artifact(case_id, artifact_key)
        if result is None:
            raise HTTPException(status_code=404, detail="Artifact not found")
        return result

    @application.get("/cases/{case_id}/findings", dependencies=secured)
    def get_case_findings(case_id: str) -> dict:
        try:
            return service.case_findings(case_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @application.get("/cases/{case_id}/changes", dependencies=secured)
    def get_case_changes(case_id: str) -> dict:
        try:
            return service.case_changes(case_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @application.get("/cases/{case_id}/proof", dependencies=secured)
    def get_case_proof(case_id: str) -> dict:
        try:
            return service.case_proof(case_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    application.state.database = database
    application.state.service = service
    application.state.worker = worker
    return application


app = create_app()
