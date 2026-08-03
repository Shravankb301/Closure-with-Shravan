# Founding FDE interview demo guide

This guide turns the project into a clear customer-and-engineering story. The
goal is not to show every feature. The goal is to demonstrate that you can sit
with an investigator, find the consequential workflow gap, ship a dependable
solution quickly, and carry what you learn back into the product.

Closure's current Founding FDE description emphasizes living with users,
shipping useful pre-scale solutions, turning frontline pain into product work,
mission alignment, ownership, ambiguity, and frequent travel. Keep every part
of the conversation anchored to those signals:

https://www.ycombinator.com/companies/closure/jobs/4gxpOAW-founding-fde

## The one-sentence pitch

> I built a source-backed evidence workspace that lets an investigator see what
> changed, trace every displayed claim to the record, preserve corrections
> without erasing history, and hand the case to the next reviewer with the
> current analytical state intact.

## The opening: 30 seconds

Say this before touching the screen:

> The customer problem I chose is not simply searching documents. It is keeping
> derived investigative work trustworthy as evidence changes. A new report,
> correction, or retraction can alter timelines and findings, and a reviewer
> needs to know what changed, why it changed, and whether anything stale is still
> visible. I built the smallest durable system I could that makes those answers
> explicit.

Then add one sentence of scope discipline:

> This is a public-record analytical demonstration, not an identification tool,
> an official law-enforcement system, or a claim of CJIS compliance.

That framing communicates user empathy and integrity before technical depth.

## The five-minute live path

### 1. Materialize the case — 40 seconds

Choose **Run guided case briefing**. Explain that four public records become 25
immutable assertions and 15 materialized entity-day timelines.

Start with the **What changed?** brief. It gives the reviewer one compact answer:
which source changed, which timelines were touched, which findings opened or
cleared, how much work was deliberately left untouched, and whether the current
state still matches a full rebuild.

Then point out three supporting details:

- allegations remain visibly distinct from court-established outcomes;
- broad source windows stay broad instead of becoming invented timestamps;
- the correctness indicator compares maintained state with a full rebuild.

Outcome line:

> The investigator gets a useful case surface, while the system retains enough
> provenance to defend how every line appeared.

FDE framing:

> I would validate this brief with an investigator first because it is the
> smallest surface that answers the question they face after every update:
> what requires my attention now?

### 2. Make uncertainty actionable — 60 seconds

Move to **Findings**. The official record has no structural contradiction, so
append the clearly labeled demonstration tip. Show that the laptop now has
mutually exclusive same-day event classes from two sources.

Say:

> This is intentionally a review prompt, not an automatic truth judgment. The
> software narrows where a human should spend attention and shows both locators.

Also point to corroboration and single-source exposure. These are more useful
to a working investigator than a generic model confidence score because they
describe the structure of the active evidence set.

### 3. Show the human-in-the-loop boundary — 60 seconds

Move to **AI intake**. The guided briefing preloads a short sample. Choose
**Extract with AI**, then show the editable proposal, verbatim source span,
time precision, and confidence.

Say:

> The model is outside the trusted derivation kernel. It can propose structure,
> but it cannot write the ledger. A reviewer must confirm the exact span and
> fields first. If the model is unavailable, intake degrades to a deterministic
> extractor instead of blocking the workflow.

Do not imply the fallback has model-level recall. Its low-confidence label is a
feature, not an embarrassment.

### 4. Correct the record — 60 seconds

Move to **Source ledger** and retract only the hypothetical tip. Accept the
correction dialog, enter a specific reason, and point out that the assigned
reviewer is snapshotted with the mutation rather than looked up from mutable
case metadata later.

Show that:

- the tip remains in the ledger with its retraction reason;
- it leaves active findings;
- the contradiction clears;
- affected timelines are recomputed and the full-rebuild check remains green.

Outcome line:

> A correction changes active reasoning without destroying the audit trail or
> forcing an expensive rebuild of unrelated case state.

### 5. Finish with ownership — 60 seconds

Move to **Officer handoff**. Enter a fictional demo assignment and a concrete
next action, for example:

> Verify the backpack disposal window against carrier and apartment access
> records; preserve the distinction between complaint allegations and the later
> sentencing record.

Save it and show the generated handoff summary.

Download the case packet and explain that it contains the current sources,
derived artifacts, findings, revision history, and rebuild proof in a portable
JSON envelope. It is intentionally simple integration plumbing: another agency
tool can consume it without scraping the interface.

Finish with:

> The deployment is not complete when the analysis renders. It is complete when
> the user can own the next decision and the next reviewer can safely continue.

## The technical deep dive

Lead with the invariant, not the component list:

> After every committed addition or retraction and after queued work drains,
> incrementally maintained artifacts must equal artifacts produced from all
> active assertions.

Then draw this sequence:

1. Append the document or retraction tombstone in a case-serialized transaction.
2. Advance only the affected entity-day change keys.
3. Queue recomputation for artifacts that actually read those keys.
4. Compute from active assertions and retain the exact lineage and read set.
5. Lock dependencies before publication and reject work if a version advanced.
6. Commit the immutable artifact version, dependencies, current pointer, and job
   success together.
7. Compare current incremental state with the deterministic full-rebuild oracle.

The strongest implementation details to discuss are:

- idempotency is checked after the case lock, preventing concurrent duplicate
  submissions from racing the unique constraint;
- PostgreSQL workers claim with `FOR UPDATE SKIP LOCKED`;
- database-time leases and per-claim fencing tokens prevent an expired worker
  from overwriting its replacement;
- stale work becomes `SUPERSEDED` rather than publishing against old inputs;
- artifact responses expose freshness and observed/current dependency versions;
- persisted job errors contain exception classes, not evidence text;
- randomized add/retract tests compare incremental state with full rebuild after
  every mutation.

## Questions you should expect

### Why this problem?

Search gets a reviewer to documents. The harder operational problem is keeping
the analytical products built from those documents correct as the source set
changes. This prototype targets that gap.

### Why not use a graph database?

The hard invariant needs transactions, row-level coordination, uniqueness, and
durable leases. PostgreSQL already provides them. The relationship graph is a
read surface over source-backed events, not the system of record.

### Why recompute findings in full?

At a single-case scale, a pure full scan is cheaper and safer than another
incremental bookkeeping layer. If profiling shows it becoming expensive, the
same findings can become versioned artifacts with declared change-key reads.

### Where does AI belong?

Upstream of the trusted kernel. Model output should be cached by document hash,
model, prompt, and schema version, shown with exact source spans, and confirmed
by a reviewer before it becomes an assertion.

### What happens when a worker crashes?

Publication is transactional. A pre-commit crash leaves no partial artifact or
dependency state. After the database-time lease expires, another fenced claim
can retry the same job.

### How would this ingest an agency's real data?

Start with its highest-value source and normalize through a deployment-specific
adapter into the assertion contract. Preserve original source identity and
locators. Build one adapter quickly, observe the investigator using it, and only
then decide which parts belong in the shared product.

### How would you know it helps?

Measure time from source arrival to reviewer-ready case state, percentage of
displayed claims with resolvable provenance, stale-artifact incidents,
correction/retraction completion time, review-queue resolution time, and user
adoption during real casework—not model output volume.

## Honest limitations

Volunteer these before someone has to discover them:

- the public-record case is manually curated and does not prove raw-file entity
  resolution;
- the prototype does not identify suspects or make investigative conclusions;
- it does not claim CJIS compliance, agency authorization, or production
  readiness for sensitive evidence;
- the free hosted profile can embed the queue worker in the web process;
- mutation actor labels snapshot case assignment metadata; they are not an
  authenticated identity claim until production SSO and role controls exist;
- semantic contradiction detection is deliberately excluded from the trusted
  path;
- SQLite exists for local convenience, while the concurrency claims depend on
  PostgreSQL tests.

Then explain why each boundary exists. Scope discipline is part of the product
judgment being evaluated.

## A credible first 90 days at an agency

### Days 1–30: observe and deliver one outcome

- shadow investigators on one live workflow;
- define the concrete decision and evidence sources;
- build one ingestion adapter and a small, labeled evaluation set;
- ship a thin vertical slice with auditability, access controls, and a runbook;
- measure time saved and provenance coverage with actual users.

### Days 31–60: harden what repeated

- add identity and role-aware access, retention policy hooks, and deployment
  observability;
- expand the evaluation set from reviewer corrections and failure cases;
- build review queues for entity resolution and low-confidence extraction;
- separate agency-specific adapters from reusable product primitives.

### Days 61–90: productize the recurring pattern

- turn repeated deployment work into supported connectors and configuration;
- establish upgrade, rollback, incident, and data-migration procedures;
- document the customer playbook so the next deployment starts faster;
- bring validated user pain and measured outcomes into the core roadmap.

## Questions to ask Closure

Choose two or three, based on the conversation:

- What is the most common point where a promising agency pilot becomes hard to
  operationalize?
- Which evidence source currently creates the most deployment-specific work?
- What behavior distinguishes investigators who adopt Closure deeply from those
  who only use it occasionally?
- How do FDE learnings become core product decisions today?
- Where should a one-off agency solution remain intentionally pre-scale, and
  where do you most want the next FDE to create a reusable primitive?
- What result would make you say after 90 days that this hire changed the
  company's deployment velocity?

## Final close

> I built this to demonstrate the kind of work I want to do at Closure: learn a
> consequential workflow directly from users, ship the smallest dependable
> solution, be explicit about uncertainty and operational boundaries, and turn
> what repeats into a stronger product.
