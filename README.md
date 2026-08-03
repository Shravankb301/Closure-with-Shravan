# Evidence Delta

A source-backed evidence operations application built on a deterministic
recomputation engine. Boston public records are one test case, not the product.

Evidence Delta applies a software build-system technique to investigation work:
derived timelines are versioned artifacts, evidence partitions are change keys,
and each artifact records the exact key versions it read. When the record
changes, the system can rebuild only affected work while proving that the result
still matches a clean rebuild from active evidence.

The application gives a reviewer one operational workspace for:

- tracing each displayed claim to an immutable source locator,
- separating complaint and indictment allegations from later court outcomes,
- traversing an evidence-to-insight graph in either direction,
- surfacing deterministic cross-source findings: contradiction candidates,
  corroborated events, and single-source exposure,
- assigning an officer or analyst and persisting handoff context,
- adding or retracting evidence without erasing the audit trail,
- verifying maintained timelines against a deterministic full rebuild, and
- inspecting the backend path from committed mutation to published artifact.

The trusted engine underneath still answers one central technical question:

> When evidence is added or retracted, can derived investigative artifacts be
> updated selectively without changing the result of a full rebuild?

The engine maintains deterministic entity-day timelines over structured
evidence. It records the exact keys each artifact reads, queues only affected
artifacts, preserves source lineage, and verifies incremental state against a
full rebuild after every mutation.

This is an analytical demonstration, not an official law-enforcement system.
It does not claim to have solved the historical Boston investigation, identify
new suspects, make investigative conclusions, or satisfy CJIS requirements.

## Why this is an application, not a case website

The browser is one client of a stateful API and worker system. A reviewer can
make a durable evidence mutation, watch targeted jobs run, inspect immutable
artifact publications and their dependency versions, retract a source without
erasing it, and verify the current state against a deterministic rebuild.

`GET /cases/{case_id}/operations` assembles that proof from database records. It
does not return a hardcoded architecture story. The Operations workspace shows:

- the latest committed evidence revision;
- affected change keys and artifacts deliberately left untouched;
- queued, running, successful, superseded, or permanently failed jobs;
- the immutable artifact version and fingerprint published by each job;
- observed and current dependency versions; and
- whether maintained state equals a full rebuild from active assertions.

The Boston record is valuable because it is a bounded, source-cited acceptance
scenario. The actual product primitive is keeping analytical work correct and
explainable as its inputs change.

## Tech stack

| Layer | Technology | Responsibility |
|---|---|---|
| Interface | Semantic HTML, modern CSS, vanilla JavaScript, and SVG | Responsive investigator workspace, evidence graph, guided demo, and review workflows without a frontend build step |
| API | Python 3.12, FastAPI, Uvicorn, and Pydantic | Typed HTTP contracts, access boundaries, validation, and operational read models |
| Persistence | SQLAlchemy 2 and Alembic | Transactional evidence ledger, immutable artifact versions, custody history, and schema migrations |
| Databases | SQLite locally and PostgreSQL 17 in deployment | Frictionless local setup with production-grade leases and concurrent worker coordination |
| Recompute worker | Durable Python worker with database leases and fencing tokens | Selective invalidation, retry-safe recomputation, and atomic artifact publication |
| Public artifact processing | HTTPX, pypdf, SHA-256 fingerprints, optional Poppler and Tesseract OCR | Allowlisted retrieval, exact-byte custody, text extraction, and cited-span verification |
| Assisted intake | Optional Anthropic adapter behind reviewer confirmation | Proposes source-quoted assertions; it never writes evidence without human approval and is excluded from deterministic derivation |
| Quality | Pytest and Ruff | Behavioral tests, regression coverage, linting, and formatting checks |
| Delivery | Docker, Render Blueprint, and a Vercel ASGI entrypoint | Reproducible local and hosted execution |

The browser has no Node.js or bundler dependency. It consumes the same FastAPI
contracts that another operational client or customer integration would use.

## Run locally

### Prerequisites

- Python 3.12 or newer
- Git
- Docker only if you want PostgreSQL locally
- Poppler and Tesseract only if you want Linux OCR outside Docker

### 1. Install the application

```bash
git clone https://github.com/Shravankb301/Closure-with-Shravan.git
cd Closure-with-Shravan
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[dev]"
```

### 2. Start the complete local workspace

SQLite is the default, so no database service or migration command is required.
The API creates and upgrades its local schema at startup. The embedded worker
keeps derived timelines and findings current while you use the interface.

```bash
PUBLIC_DEMO_MODE=true \
RUN_EMBEDDED_WORKER=true \
ENABLE_LOCAL_OCR=false \
.venv/bin/uvicorn evidence_delta.api:app --reload
```

Open [http://localhost:8000](http://localhost:8000), then select **Open
demonstration case** or **Start guided demo**. Set `ENABLE_LOCAL_OCR=true` when
your machine has a supported local OCR adapter. The application still records
and exposes OCR constraints when OCR is disabled.

### 3. Run the checks

```bash
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

### Optional: run with PostgreSQL

```bash
docker compose up -d
DATABASE_URL=postgresql+psycopg://evidence_delta:evidence_delta@localhost:5432/evidence_delta \
PUBLIC_DEMO_MODE=true \
RUN_EMBEDDED_WORKER=true \
.venv/bin/uvicorn evidence_delta.api:app --reload
```

Interactive API documentation is available at
[http://localhost:8000/docs](http://localhost:8000/docs).

## Engine-only scenario

To run the deterministic engine scenario without the web application:

```bash
.venv/bin/python -m evidence_delta.demo
```

The scenario creates 100 entity-day artifacts, adds one document that affects three,
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

For a reviewer, the fastest path is the **Start guided demo** action on
the opening screen. It walks through the case state, cross-source findings,
the live backend trace, review-gated assisted intake, append-only retraction, and
officer handoff without requiring prior knowledge of the architecture.

## Documentation map

| Document | Use it for |
|---|---|
| [README.md](README.md) | Product framing, local setup, and capability overview |
| [INTERVIEW_DEMO.md](INTERVIEW_DEMO.md) | The outcome-first live demo and likely FDE follow-ups |
| [DESIGN.md](DESIGN.md) | Invariants, transaction boundaries, worker behavior, and verification |
| [ARCHITECTURE_REVIEW.md](ARCHITECTURE_REVIEW.md) | Alternatives considered, failure modes, and remaining risks |

## Repository guide

| Path | Responsibility |
|---|---|
| `src/evidence_delta/api.py` | HTTP routes, authentication boundaries, and error mapping |
| `src/evidence_delta/service.py` | Transactional case mutations and read models |
| `src/evidence_delta/worker.py` | Durable job claiming, recomputation, and atomic publication |
| `src/evidence_delta/analysis.py` | Deterministic cross-source finding rules |
| `src/evidence_delta/evidence_graph.py` | Server-side evidence-to-insight node and relationship mapping |
| `src/evidence_delta/public_artifacts.py` | Allowlisted public-source retrieval, fingerprinting, extraction, and span verification |
| `src/evidence_delta/extraction.py` | Human-confirmed evidence extraction workflow |
| `src/evidence_delta/models.py` | Persistent ledger, dependency, job, and artifact records |
| `src/evidence_delta/settings.py` | Environment-backed application configuration |
| `src/evidence_delta/static/investigation.html` | Operator workspace and guided demonstration |

## Case workspace

The root route opens **Evidence Delta**. The demonstration action attempts to
retrieve four official artifacts, records their acquisition status and content
fingerprints, and organizes 25 reviewed assertions into 15 entity-day timelines. The
workspace renders live case metrics, an
inspectable evidence-to-insight map, an event chronology, a legal-status distribution,
an officer review queue, a source ledger, evidence intake, persistent case
assignment/handoff metadata, and a findings board that surfaces contradiction
candidates, corroboration, and single-source exposure across the active
source set. Its Operations view exposes the durable execution records behind
the current screen.

### Official-record use case

The **Open demonstration case** action creates a durable workspace for the narrow
evidence-disposal obstruction case associated with the Boston Marathon bombing
investigation. It materializes 25 source-backed assertions into 15 timelines
from four official records:

- [May 2013 criminal complaint](https://www.justice.gov/iso/opa/resources/628201351145721158286.pdf)
- [August 2013 indictment announcement](https://www.justice.gov/usao-ma/pr/federal-grand-jury-indicts-two-men-obstruction-justice-boston-marathon-bombing)
- [FBI case history](https://www.fbi.gov/history/cases-and-criminals/boston-marathon-bombing)
- [June 2015 sentencing record](https://www.justice.gov/usao-ma/pr/dias-kadyrbayev-sentenced-six-years-impeding-boston-marathon-bombing-investigation)

The acquisition service allowlists official DOJ and FBI hosts, follows redirects,
limits artifact size, records HTTP and content metadata, hashes returned bytes,
and attempts HTML or PDF text extraction. It explicitly reports anti-bot
challenges and scanned PDFs that require OCR. Those constraints are never
presented as successful machine reading. The stable demonstration uses a
reviewed, source-located assertion set after acquisition so downstream
ingestion, recomputation, and evidence mapping remain reproducible.

Complaint and indictment events remain labeled as allegations. The interface
uses court-established labels only for events reported after a guilty plea or
jury verdict. Coarse source times retain `DAY`, `MONTH`, or `WINDOW` precision
instead of presenting invented exact timestamps. The resulting case is a
normal workspace: a reviewer can add evidence, inspect source spans and
lineage, share the case URL, or retract a source.

### Deterministic cross-source findings

`GET /cases/{case_id}/findings` derives three kinds of review findings from the
active (non-retracted) assertion set:

- **Contradiction candidates.** Two active sources assert mutually exclusive
  event classes for the same entity on the same day (for example `disposal`
  vs `concealment`). The conflict table is an explicit, deliberately minimal
  allowlist; a flag is a review prompt for the assigned officer, never an
  automatic falsehood determination.
- **Corroborated events.** The same entity-day event class is supported by two
  or more distinct source records, with cross-tier support (allegation-tier and
  court-established records agreeing) called out explicitly.
- **Single-source exposure.** Entity-days whose every active event rests on one
  source record, highlighting the leads most in need of corroboration.

Detection is structural: entity, day, kind-derived event class, source
identity, and legal tier. There is no free-text comparison and no model call in
this path, so the same active assertion set always produces byte-identical
findings. Event classes come from an explicit kind-suffix allowlist, so a new
assertion kind never silently joins a conflict rule.

The curated official record is internally consistent, so it produces zero
contradictions, which is itself the correct finding. The Findings panel
offers a clearly labeled hypothetical demonstration tip that conflicts with
the court-established laptop concealment; appending it surfaces a live
contradiction with both cited locators, and retracting it clears the flag
while the tip remains in the audit ledger.

Findings are recomputed in full on every read. They are a pure function of
active assertions, so they inherit full-rebuild semantics without incremental
machinery; if they became expensive they would become artifacts with change
keys exactly like timelines.

### Live evidence-mapping service

`GET /cases/{case_id}/evidence-graph` reads active assertions and provenance
from the persistent ledger, derives current findings, and maps them into a
deterministic directed graph:

```text
document -> source-backed assertion -> entity or artifact -> review finding
```

Documents, assertions, people, locations, physical artifacts, corroborated
findings, conflicts, and missing-support gaps are separate node types. Selecting
any node exposes both its inputs and its downstream uses, so a reviewer can move
from a conclusion to every cited locator or start with a source and see every
finding it influences.

Each finding includes the exact deterministic rule ID, the test that ran, the
premises that satisfied it, and a structural support level. Levels such as
`STRONG`, `MODERATE`, `LIMITED`, and `CONFLICTED` are not probabilities or model
confidence scores. They summarize source independence, legal-status breadth,
and known conflict or sourcing gaps.

The browser renders this API contract. It does not infer evidence relationships
from page copy or a Boston-specific graph fixture. Adding a source changes the
returned nodes and edges at the new case revision; retracting that source
removes it from active reasoning while preserving its ledger record. The graph
contains no invented semantic edges and no hidden model reasoning.

### Use your own evidence

The browser also exposes a durable case workspace intended for hands-on review.
A user can create or reopen a case, add a guided raw-text observation, upload or
paste JSON/CSV assertions, inspect every materialized timeline and source span,
share the case URL, and retract a source without deleting its audit record.

JSON and CSV inputs use these fields:

```text
entity_id, occurred_at, kind, value, time_precision, source_locator, source_text
```

The first four fields are required. `time_precision` defaults to `EXACT`,
`source_locator` defaults to the input row, and `source_text` defaults to
`value`. A document envelope may also include an absolute `source_uri`.
Uploaded bytes are parsed in the browser; the durable record is the validated
assertion set and its provenance, not the original file blob. Imports are
limited to 1,000 assertions and 5 MB in the hosted workspace.

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
    B --> H["Evidence graph API"]
    H --> I["Interactive reasoning map"]
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
- `source_acquisition_attempts` form a per-document hash chain for public fetches
  and reviewer imports.
- exact retrieved bytes are stored by SHA-256 in a content-addressed artifact
  vault when `ARTIFACT_VAULT_DIR` is configured.
- `change_sets.performed_by` snapshots the assigned reviewer at mutation time;
  later reassignment cannot rewrite the historical actor.
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
| `POST` | `/cases` | Create an isolated durable case |
| `GET` | `/cases/{case_id}` | Reopen a case with its source history and current artifacts |
| `PUT` | `/cases/{case_id}/assignment` | Persist assigned officer, unit, and handoff context |
| `POST` | `/demo/real-case/boston-obstruction?acquire_public_sources=true` | Fetch official artifacts and materialize the reviewed public-record case |
| `POST` | `/cases/{case_id}/documents` | Add structured assertions idempotently |
| `POST` | `/cases/{case_id}/documents/{document_id}/retractions` | Append a retraction tombstone |
| `POST` | `/workers/drain` | Process queued recomputations locally |
| `GET` | `/cases/{case_id}/artifacts/{artifact_key}` | Read a versioned artifact and lineage |
| `GET` | `/cases/{case_id}/findings` | Derive contradiction candidates, corroboration, and single-source exposure |
| `GET` | `/cases/{case_id}/evidence-graph` | Map active sources, assertions, entities, and findings into labeled relationships |
| `GET` | `/cases/{case_id}/source-acquisitions` | Inspect retrieval, fingerprint, extraction, verification, and ingestion metadata |
| `POST` | `/cases/{case_id}/source-acquisitions/{document_id}/imports` | Reprocess a browser-retrieved artifact and append its custody attempt |
| `GET` | `/cases/{case_id}/search?q=laptop+concealment` | Search active assertions and return ranked source excerpts and stable locators |
| `GET` | `/cases/{case_id}/changes` | Explain recent source mutations, affected timelines, finding deltas, and recomputation state |
| `GET` | `/cases/{case_id}/operations` | Inspect the live mutation pipeline, worker jobs, publications, dependencies, and selectivity |
| `GET` | `/cases/{case_id}/proof` | Verify full-rebuild equivalence and inspect live proof counts |

Interactive API documentation is available at `/docs` while the server runs.

Artifact processing uses on-device macOS Vision OCR locally and automatically
falls back to Poppler plus Tesseract when those binaries are installed. The
included Docker image installs the cross-platform OCR runtime used by Render.
Set `ENABLE_LOCAL_OCR=false` to disable OCR. Exact source bytes default to
`./.evidence_delta_artifacts`; set
`ARTIFACT_VAULT_DIR` to a mounted encrypted volume or managed object-storage
adapter in production.

## Deploy on Render

The repository includes a Render Blueprint for one web service and one managed
PostgreSQL 17 database:

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Shravankb301/Closure-with-Shravan)

The Blueprint creates a public, non-sensitive interview demonstration. It uses
the Docker image so scanned PDFs receive Tesseract OCR on Linux, runs Alembic
before every deploy, and deploys each commit to the linked branch. The database
blocks external network connections and the application uses Render's internal
connection string.

Do not use the public-demo profile for customer evidence. For a private hosted
deployment, set `PUBLIC_DEMO_MODE=false`, configure `DEMO_API_KEY` and optionally
`DEMO_READ_ONLY_KEY`, and provide an authenticated client instead of exposing
mutation controls publicly.

The free demo profile deliberately runs the durable queue worker in the web
process because Render does not offer free background workers. For a production
split, set `RUN_EMBEDDED_WORKER=false` on the web service and run
`evidence-delta-worker` as a separate worker service using the same
`DATABASE_URL`.

Free Render web services sleep after inactivity, and free Render Postgres
databases expire after 30 days. That is acceptable for a short-lived interview
demo, not for retained evidence or production use. The free profile also stores
artifact bytes on ephemeral container storage; recreating the demo case restores
them after an instance replacement. Production deployments require a persistent
encrypted volume or object-storage adapter.

## Design notes and limitations

### Determinism is required

Artifact derivation is a pure function of a fixed assertion set. No LLM call is
allowed in the trusted derivation path. A future extraction stage may use a
model, but its output must be cached by document hash, model version, prompt
version, and schema version before entering this kernel.

### Entity resolution is upstream

Fixtures contain curated entity IDs. Entity resolution is treated as an
upstream oracle because uncertain merges and splits are a separate hard problem
that would obscure the incremental-computation experiment.

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
production identity system. Structured assertions keep the central correctness
property testable and explainable; curated public-record assertions exercise
the same kernel without implying automated extraction or case solving.

See [DESIGN.md](DESIGN.md) for failure modes and transactional details, and
[ARCHITECTURE_REVIEW.md](ARCHITECTURE_REVIEW.md) for challenged alternatives,
lock ordering, retry states, and assumptions that remain unsafe.
