from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from evidence_delta.database import Database
from evidence_delta.schemas import CaseInput, DocumentInput, RetractionInput
from evidence_delta.service import EvidenceService
from evidence_delta.worker import RecomputeWorker


def create_app(database_url: str | None = None) -> FastAPI:
    url = database_url or os.getenv("DATABASE_URL", "sqlite:///./evidence_delta.db")
    database = Database(url)
    service = EvidenceService(database)
    worker = RecomputeWorker(database)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        database.create_schema()
        yield

    application = FastAPI(
        title="Evidence Delta Engine",
        version="0.1.0",
        lifespan=lifespan,
    )

    @application.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @application.post("/cases", status_code=201)
    def create_case(body: CaseInput) -> dict:
        record = service.create_case(body.name)
        return {"id": record.id, "name": record.name, "revision": record.revision}

    @application.post("/cases/{case_id}/documents", status_code=202)
    def add_document(case_id: str, body: DocumentInput) -> dict:
        try:
            return service.ingest_document(case_id, body).model_dump(mode="json")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @application.post("/cases/{case_id}/documents/{document_id}/retractions", status_code=202)
    def retract_document(case_id: str, document_id: str, body: RetractionInput) -> dict:
        try:
            return service.retract_document(case_id, document_id, body.reason).model_dump(
                mode="json"
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @application.post("/workers/drain")
    def drain_worker() -> dict:
        results = worker.run_until_idle()
        return {
            "processed": len(results),
            "artifacts": [item.model_dump(mode="json") for item in results],
        }

    @application.get("/cases/{case_id}/artifacts/{artifact_key:path}")
    def get_artifact(case_id: str, artifact_key: str) -> dict:
        result = service.current_artifact(case_id, artifact_key)
        if result is None:
            raise HTTPException(status_code=404, detail="Artifact not found")
        return result

    application.state.database = database
    application.state.service = service
    application.state.worker = worker
    return application


app = create_app()
