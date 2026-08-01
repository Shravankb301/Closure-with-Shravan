# Design review notes

## Invariant

After every committed addition or retraction and after queued work drains:

```text
incremental artifacts = artifacts produced from all active assertions
```

The test suite checks this after 300 randomized mutations, not only after a
curated example.

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
