# Design review notes

This document is the implementation-level reference for invariants,
transactions, and worker behavior. See [README.md](README.md) for setup and
[ARCHITECTURE_REVIEW.md](ARCHITECTURE_REVIEW.md) for challenged alternatives and
remaining risks.

## Invariant

After every committed addition or retraction and after queued work drains:

```text
incremental artifacts = artifacts produced from all active assertions
```

The test suite checks this after 300 randomized mutations, not only after a
curated example.

## Operator proof surface

`GET /cases/{case_id}/operations` exposes the invariant as live operational
data. The response is assembled from change sets, recomputation jobs, artifact
versions, artifact dependencies, change keys, and the rebuild oracle. The UI
does not infer success from the presence of a rendered timeline.

For the latest case revision it reports:

1. whether the evidence mutation exists in the durable revision ledger;
2. how many artifact keys were affected and how many were left untouched;
3. which durable jobs targeted the revision and their attempts and status;
4. which immutable artifact version each successful job published;
5. whether observed dependency versions still match current key versions; and
6. whether incremental state equals a clean rebuild from active assertions.

This turns backend correctness into a product capability that an operator can
inspect during a live evidence change.

## Evidence mapping service

`GET /cases/{case_id}/evidence-graph` is a server-side projection over active
ledger rows. The mapper joins each immutable assertion to its source document,
maps it to an entity, and connects it to every deterministic finding that reads
it. It also emits summary edges for views that collapse assertion nodes. Node
and edge identities are deterministic, and the response declares the case
revision and active input counts used to build it.

The browser is a renderer of this contract. It can filter or reposition nodes,
but it does not create evidence relationships. Retraction changes the active
projection while the underlying document and assertions remain in the audit
ledger.

## Public artifact acquisition

The guided case can invoke an allowlisted connector before ingestion. Each
official URL is fetched with redirect, timeout, and size limits. The acquisition
record stores the resolved URL, HTTP status, content type, byte count, SHA-256
fingerprint, extraction method, page or character counts, and source-span
verification result. Exact returned bytes are written to a content-addressed
artifact vault and rehashed when the custody report is read.

Acquisition failure is data, not an exception hidden from the operator. An
anti-bot interstitial becomes `ACCESS_CHALLENGE`; a scanned PDF is routed to the
configured OCR adapter. On macOS, the local adapter renders each PDF page and
uses the on-device Vision framework. Linux deployments use Poppler and
Tesseract from the included container image. OCR span checking permits narrowly bounded
character substitutions while still rejecting reviewed paraphrases.

A reviewer can download a challenged source through an approved authenticated
browser and import the exact file. The replacement acquisition snapshot is
promoted only after fingerprinting, extraction, and source-span verification.
The original fetch and every later import remain in an append-only custody
chain whose event hash includes the previous event hash.

## Active evidence search

`GET /cases/{case_id}/search` ranks normalized, stemmed, and typo-tolerant terms
across active assertion values, entities, event types, source records, source excerpts, and locators. Results
always include the exact stored excerpt and stable source locator. The search
does not produce a generated answer, and the active-evidence query excludes
retracted source records immediately.

## Hosted access roles

The hosted demonstration supports separate reviewer and viewer keys. A viewer
may read case state, findings, search results, custody reports, and operational
proof. Mutations require the reviewer key. This is useful least privilege for
an interview deployment, not a substitute for customer SSO, user lifecycle
management, or policy-based authorization.

## Mutation path

1. Lock the case row and advance its revision.
2. Check idempotency after acquiring the lock, so concurrent duplicates return
   one logical document instead of racing a unique constraint.
3. Append a document or a document-retraction tombstone.
4. Derive the affected entity-day change keys.
5. Advance only those key versions.
6. Find artifacts through their recorded dependency reads.
7. Create one idempotent recomputation job per affected artifact and revision.
8. Persist a change set containing affected and untouched counts.

## Worker path

1. Claim one available or lease-expired job with `FOR UPDATE SKIP LOCKED`.
   Lease timestamps come from the database, and every claim receives a new
   fencing token.
2. Read active assertions for the artifact key.
3. Read the dependency version, inputs, and dependency version again. If the
   version changed, supersede the job instead of combining inconsistent reads.
4. Derive payload, exact lineage, input fingerprint, and dependency read set.
5. Lock every dependency key and verify its observed version is still current.
6. Verify the fencing token still owns the job. A resumed expired worker cannot
   publish or overwrite the replacement worker's state.
7. In one transaction, append the artifact version and dependencies, move the
   current pointer, and mark the job successful. Publication idempotency uses
   the job ID, not output equality, so an equivalent state at a later revision
   still records a fresh dependency observation.
8. If the transaction rolls back, the job lease expires and the same work can
   be retried without orphan rows.

## Four failure modes to whiteboard

### 1. Missed invalidation from an unrecorded read

If a deriver reads data without recording its key, a later change can leave the
artifact silently stale. Derivers therefore return their read set as part of the
computation result. A production registry should reject artifact publication if
the declared key shape does not match the deriver type. When dependency scope is
uncertain, invalidate a broader partition rather than risk a false negative.

### 2. Stale publication during a concurrent mutation

A worker can compute from key version 12 while another transaction advances it
to 13. The worker locks its observed keys immediately before publication and
compares versions. If one changed, the job becomes `SUPERSEDED`; the mutation
that advanced the key already enqueued replacement work in the same transaction.
The race is forced deterministically in the test suite.

### 3. Orphaned state after retraction

Deleting assertions would destroy provenance and could strand derived rows.
Retraction instead appends a tombstone, advances every key touched by the source
document, and recomputes those artifacts from the remaining active assertions.
The original source assertions remain auditable.

### 4. Nondeterminism breaks the oracle

The equivalence oracle is meaningful only when derivation is deterministic.
Timeline derivation is pure and rule-based. If a model is introduced upstream,
its structured output must be frozen by an input-and-version cache key before
the assertion set reaches this engine.

## Why PostgreSQL is enough

The prototype needs transactions, uniqueness constraints, row-level claims, and
durable leases. PostgreSQL already supplies those. Redis, Celery, and a graph
database would add operational surfaces without strengthening the invariant.
SQLite remains available only for a zero-setup local demonstration and tests;
it does not provide PostgreSQL's concurrent `SKIP LOCKED` behavior.

CI therefore starts PostgreSQL and proves that four workers claim twenty queued
jobs exactly once each. A separate PostgreSQL test submits the same document
from two concurrent transactions and verifies one idempotent logical result.

## Retry policy

Deterministic application failures become permanent after one attempt.
Explicit transient failures, connection failures, timeouts, and SQLAlchemy
operational errors may consume at most three attempts. Persisted errors contain
only the exception class. Immediate retry keeps the kernel small, but production
would need backoff, jitter, `next_attempt_at`, and a dead-letter review path.

## Reader contract

An artifact response includes `fresh` and the observed/current version for each
dependency. A pending or permanently failed recomputation therefore cannot make
an older artifact look current. This exposes staleness; it does not decide
whether a caller should block, warn, or deliberately use the older version.

## Deployment boundary

PostgreSQL schema changes are applied through a frozen Alembic migration before
the web process starts. Provider connection strings are normalized to psycopg 3,
the engine checks pooled connections before reuse, and `/health` verifies a real
database round trip.

The free interview deployment embeds the queue poller in the web process. This
preserves transaction, lease, fencing, and retry semantics, but it is an
operational compromise: a sleeping web instance cannot process work. A paid or
production deployment should run the same `evidence-delta-worker` entry point as
an independently scalable worker service.

## Findings are derived on read

Cross-source findings (contradiction candidates, corroboration, single-source
exposure) are a pure function of the active assertion set, computed in full on
every `GET /cases/{id}/findings`. This is deliberate: at case scale the full
computation is cheaper than the bookkeeping an incremental version would need,
and recomputing from scratch means findings can never be stale relative to a
retraction. If findings became expensive, they would become artifacts with
declared change-key dependencies and flow through the same worker, versioning,
and full-rebuild oracle as timelines.

Conflict detection is structural and rule-based. Event classes come from an
explicit kind-suffix allowlist, and the conflict table is a minimal set of
mutually exclusive class pairs. Free-text semantic comparison is excluded from
this trusted path for the same reason LLM calls are excluded from derivation:
the kernel's outputs must be reproducible and explainable to a reviewer. A
model-assisted contradiction detector belongs upstream, proposing structured
assertions that a human confirms before they enter the kernel.

## HTTP caching is disabled for evidence reads

Case, artifact, proof, and findings responses are marked `Cache-Control:
no-store`, and the browser client requests with `cache: "no-store"`. Without
this, a heuristically cached GET can show a reviewer a retracted source as
still active, observed in practice as a reloaded case rendering one evidence
revision behind the database. Only the static social-preview image is cacheable.
