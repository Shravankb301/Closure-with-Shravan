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

## Interactive engineering proof

The root route turns the 3-of-100 scenario into a guided live proof. One action
materializes 100 independent entity-day artifacts, highlights the exact three
invalidated partitions, publishes their replacement versions, renders source
lineage and dependency observations, and verifies every maintained artifact
against a deterministic full rebuild. The same source can then be retracted to
show that derived state changes while all three original assertions and prior
artifact versions remain auditable.

The interface also explains the failure envelope covered by automated tests:
crash-before-commit rollback, stale-worker fencing, bounded retries, and
PostgreSQL multi-worker claims. Its link-preview image uses the same impact map,
so a shared demo URL communicates the result before the page opens.

The hosted demo can require an `X-Demo-Key`. The browser keeps that value in
session storage only and never places it in a URL.

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

Before publication, the worker locks every dependency key in sorted order and
compares its current version with the version observed during computation. If a
key advanced, the stale job becomes `SUPERSEDED` and cannot move the artifact's
current pointer. A per-claim fencing token also prevents a lease-expired worker
from overwriting its replacement. Deterministic failures stop immediately;
classified transient failures have a bounded attempt budget. Persisted errors
contain only exception classes so evidence text does not leak into job records.

Artifact responses expose `fresh` plus observed and current dependency versions.
Pending or permanently failed recomputation therefore remains visible instead
of silently presenting an old artifact as current.

## Data semantics

- `documents` are idempotent within a case by canonical content hash.
- `assertions` are immutable, source-specific statements, not authoritative facts.
- `document_retractions` are append-only tombstones. Retraction never deletes an assertion.
- `change_keys` identify the smallest supported recomputation partition.
- `artifact_versions` are immutable results with exact source lineage.
- `artifact_dependencies` record the keys and versions actually read.
- `recompute_jobs` use database-time leases, fencing tokens, and transactional
  publication for crash recovery.

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
| `GET` | `/cases/{case_id}/proof` | Verify full-rebuild equivalence and inspect live proof counts |

Interactive API documentation is available at `/docs` while the server runs.

## Deploy on Render

The repository includes a Render Blueprint for one web service and one managed
PostgreSQL 17 database:

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Shravankb301/Closure-with-Shravan)

During Blueprint creation, set a strong `DEMO_API_KEY`. The database blocks
external network connections and the application uses Render's internal
connection string. Alembic runs before every deploy, health checks verify the
database, and automatic deploys wait for GitHub checks to pass.

The free demo profile deliberately runs the durable queue worker in the web
process because Render does not offer free background workers. For a production
split, set `RUN_EMBEDDED_WORKER=false` on the web service and run
`evidence-delta-worker` as a separate worker service using the same
`DATABASE_URL`.

Free Render web services sleep after inactivity, and free Render Postgres
databases expire after 30 days. That is acceptable for a short-lived interview
demo, not for retained evidence or production use.

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

### Concurrent mutations

Mutations serialize on the case row, then advance affected keys and enqueue work
in the same transaction. Workers use key-level optimistic publication checks,
not the whole `case_revision`, so unrelated evidence does not reject valid work.
The test suite forces a mutation precisely between compute and publish and
verifies that the stale version never appears.

### Deliberate exclusions

Raw PDF ingestion, OCR, audio/video processing, entity resolution, natural
language question answering, role-based authorization, and disaster recovery
are outside scope. The hosted API key is controlled-demo access, not a
production identity system. Synthetic structured assertions keep the central
correctness property testable and explainable.

See [DESIGN.md](DESIGN.md) for failure modes and transactional details, and
[ARCHITECTURE_REVIEW.md](ARCHITECTURE_REVIEW.md) for challenged alternatives,
lock ordering, retry states, and assumptions that remain unsafe.
