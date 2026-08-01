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
2. Append a document or a document-retraction tombstone.
3. Derive the affected entity-day change keys.
4. Advance only those key versions.
5. Create one idempotent recomputation job per affected artifact and revision.
6. Persist a change set containing affected and untouched counts.

## Worker path

1. Claim one available or lease-expired job with `FOR UPDATE SKIP LOCKED`.
2. Read active assertions for the artifact key.
3. Derive payload, exact lineage, input fingerprint, and dependency read set.
4. In one transaction, append the artifact version and dependencies, move the
   current pointer, and mark the job successful.
5. If the transaction rolls back, the job lease expires and the same work can
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
to 13. Before publication, a production worker must lock every observed key and
verify its version is unchanged. If verification fails, it should discard the
result and enqueue a retry. This check is documented but deliberately not built
in the kernel.

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
