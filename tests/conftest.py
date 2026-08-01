from __future__ import annotations

import pytest

from evidence_delta.database import Database
from evidence_delta.service import EvidenceService
from evidence_delta.worker import RecomputeWorker


@pytest.fixture
def database() -> Database:
    database = Database("sqlite+pysqlite:///:memory:")
    database.create_schema()
    return database


@pytest.fixture
def service(database: Database) -> EvidenceService:
    return EvidenceService(database)


@pytest.fixture
def worker(database: Database) -> RecomputeWorker:
    return RecomputeWorker(database, lease_seconds=0)
