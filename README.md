# Evidence Delta Engine

A small backend prototype for one question:

> When evidence is added or retracted, can derived investigative artifacts be
> updated selectively without changing the result of a full rebuild?

The engine maintains deterministic entity-day timelines over synthetic,
structured evidence. It records the exact keys each artifact reads, queues only
affected artifacts, preserves source lineage, and verifies incremental state
against a full rebuild after every mutation.

This is an incremental-computation experiment, not an investigation platform.
It does not process real evidence and does not claim CJIS compliance.

## The demonstration

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/python -m evidence_delta.demo
```

`demo` creates 100 entity-day artifacts, adds one document that affects three,
retracts it without deleting its assertions, simulates a worker crash before
commit, and runs 300 randomized additions and retractions. After every random
mutation, it asserts:

```text
incrementally maintained state == state produced by a full rebuild
```

Representative impact:

```json
{
  "affected_artifacts": 3,
  "untouched_artifacts": 97,
  "equivalence_checked_after_each_mutation": true,
  "assertions_deleted_after_retraction": 0,
  "retry_published_artifact_version": 1
}
```

Timing in the demo is intentionally labeled as a local illustration, not a
production-scale benchmark. Correctness is the primary result.

## Architecture

```mermaid
flowchart TD
    A["Structured evidence"] --> B["Append-only assertions"]
    B --> C["Versioned change keys"]
    C --> D["Postgres job queue"]
    D --> E["Pure timeline deriver"]
    E --> F["Artifact version + lineage"]
    F --> G["Full-rebuild oracle"]
```

The worker claims jobs with `SELECT ... FOR UPDATE SKIP LOCKED` when running on
PostgreSQL. Artifact version, dependency read set, current-version pointer, and
job completion are committed in one transaction. A crash before commit leaves
no artifact versions or dependency rows behind, and the expired lease makes the
same job retryable.

## Data semantics

- `documents` are idempotent within a case by canonical content hash.
- `assertions` are immutable, source-specific statements, not authoritative facts.
- `document_retractions` are append-only tombstones. Retraction never deletes an assertion.
- `change_keys` identify the smallest supported recomputation partition.
- `artifact_versions` are immutable results with exact source lineage.
- `artifact_dependencies` record the keys and versions actually read.
- `recompute_jobs` use leases and transactional publication for crash recovery.

## API

SQLite is the frictionless local default:

```bash
.venv/bin/uvicorn evidence_delta.api:app --reload
```

For PostgreSQL:

```bash
docker compose up -d
export DATABASE_URL=postgresql+psycopg://evidence_delta:evidence_delta@localhost:5432/evidence_delta
.venv/bin/uvicorn evidence_delta.api:app --reload
```

Core endpoints:

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/cases` | Create an isolated synthetic case |
| `POST` | `/cases/{case_id}/documents` | Add structured assertions idempotently |
| `POST` | `/cases/{case_id}/documents/{document_id}/retractions` | Append a retraction tombstone |
| `POST` | `/workers/drain` | Process queued recomputations locally |
| `GET` | `/cases/{case_id}/artifacts/{artifact_key}` | Read a versioned artifact and lineage |

Interactive API documentation is available at `/docs` while the server runs.

## Design notes and limitations

### Determinism is required

Artifact derivation is a pure function of a fixed assertion set. No LLM call is
allowed in the trusted derivation path. A future extraction stage may use a
model, but its output must be cached by document hash, model version, prompt
version, and schema version before entering this kernel.

### Entity resolution is upstream

Synthetic inputs contain ground-truth entity IDs. Entity resolution is treated
as an upstream oracle because uncertain merges and splits are a separate hard
problem that would obscure the incremental-computation experiment.

### Concurrent case revisions

The current worker records the case revision and exact change-key versions it
observed, but it does not yet reject publication when one of those keys advances
during computation. The production fix is an optimistic publication check:
lock the observed keys, compare their versions, and retry instead of publishing
if any changed. Using the whole `case_revision` would be safe but would cause
unnecessary retries for unrelated evidence.

### Deliberate exclusions

Raw PDF ingestion, OCR, audio/video processing, entity resolution, natural
language question answering, authentication, and a user interface are outside
scope. Synthetic structured assertions keep the central correctness property
testable and explainable.

See [DESIGN.md](DESIGN.md) for failure modes and transactional details.
