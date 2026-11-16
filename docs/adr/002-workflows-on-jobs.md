# ADR 002: Workflows implemented on the jobs table

## Status

Accepted

## Context

Phase 2 needs durable multi-step workflows: ordered steps, outputs flowing
forward, per-step retries, resumability after crashes, and dead-letter replay.
Phase 1 already provides a Postgres-backed queue with `SKIP LOCKED` claiming,
`(locked_by, attempts)` fencing, heartbeats, a reaper, and full-jitter backoff.

## Decision

**Every step is an ordinary job.** A workflow is a `workflows` row plus
`workflow_steps` rows (the per-instance snapshot), and each step executes as a
`workflow_step` job carrying `{workflow_id, position}` with
`dedupe_key = 'wf:<id>:step:<pos>'`. No separate execution engine, scheduler,
or state machine process is introduced; the queue's existing recovery
mechanics (lease, reaper, backoff, dead) are reused as-is.

### The step-complete transaction

The core invariant: **the step's output and the carrying job's completion
commit in one transaction, with `complete` last under the full claim token.**

Order inside `with conn.transaction():` on the autocommit connection:

1. `jobs.complete(...)` — the fence; zero rows (`LeaseLost`) rolls everything
   back, discarding the output.
2. `UPDATE workflow_steps SET state = 'succeeded', output = ...`
3. Enqueue the next step's job (dedupe key makes it idempotent), or mark the
   workflow `succeeded` after the last step.

Rationale (crash analysis): with two transactions, **complete-first** is the
unrecoverable ordering — a crash between them leaves a succeeded job with a
pending step and a lost output; the step's dedupe key then returns the
succeeded job forever and DLQ replay refuses non-dead jobs, so the workflow is
stuck in `running` permanently. Output-first is self-healing (the reaper
requeues; resume skips completed steps), so one transaction with complete last
under the fence gives both: a lost completion is recoverable, a lost output is
impossible.

### The `after_complete` hook

The step handler runs outside any transaction and registers callbacks via
`ctx.after_complete(cb)`. The worker runs them inside the same transaction as
`complete`. Contract: the callback receives the worker's connection, uses only
that connection, and must not commit or open its own transaction. Any callback
exception rolls back the whole transaction (including the `complete`) and the
job is failed.

### Lock order

Every multi-row writer takes row locks in one global order: **the job row,
then the step row, then the workflow row** (the next-job INSERT last). The
advance transaction, the reconciler, and replay all follow it.

**Checkpoint 8 addition — the patch-log append.** The patch log's head
advance is a compare-and-swap (`UPDATE workspaces SET head_seq = head_seq +
1, head_hash = :new WHERE id = :ws AND head_hash = :parent RETURNING
head_seq`), not a separate lock — it adds no new edge to the global order.
The row lock that UPDATE takes on the workspaces row sits **after the job,
step and workflow locks** (it is the last statement inside the step's
advance transaction), so an append never runs before a writer that could
also want the workspace row: the order stays job -> step -> workflow ->
workspaces row, total and cycle-free.

**Exception — `start_workflow` inserts in reverse order** (workflow row, then
the step rows, then step 0's job). It is safe because those rows do not exist
for any other transaction until commit: no other writer can hold or await
them, and lock ordering only matters between transactions that touch
*existing* rows. A concurrent `start_workflow` with the same dedupe key
serializes on the unique index wait and lands on the conflict path. Write it
down, or a reviewer will flag it — which is this paragraph.

The reconciler implements the order as **separate statements inside one
transaction** (lock the dead jobs, then update steps, then update workflows)
rather than one multi-CTE statement, because PostgreSQL does not document the
execution order of data-modifying CTEs; statement order in a transaction is
guaranteed and row locks are held until commit.

`DeadlockDetected` is caught: the reconciler skips the rule (the next cycle
retries it from scratch), replay retries the whole transaction exactly once.
A test runs replay against the reconciler on the same workflow and asserts
the final state is always one of the two legal whole-outcomes.

### Workflow-failure marking: reconciler only

A workflow goes `failed` when its current step's job goes `dead` — and the
**only** writer of that transition is the reconciler, on its maintenance
cadence. The worker does not mark the workflow inside `fail()` even though
it usually has the context, because the reaper can kill a job without any
worker involved; one repair path owns the transition. The cost is latency:
at most one reconciler cycle — the interval defaults to 5 s — plus the
cycle's own statement time. Chaos v2 (checkpoint 6) measures the observed
delay; if it ever matters in practice, a synchronous mark can be added as an
optimization without changing who owns the invariant. `NonRetryableError`
flows through the same path (it is just `force_dead` in `fail.sql`, which
makes the job dead immediately — the reconciler picks it up on its next
cycle).

### Retention must not free live dedupe keys

A future retention job may only delete jobs whose workflow is terminal.
Deleting a `workflow_step` job of a running workflow frees the step's dedupe
key, and the reconciler's rule 2 ("current step pending, no job row at all →
re-enqueue") would then create a duplicate job for a step that may have
already run; the effect stays idempotent but the workflow gets a second era
of the same step, and rule 3's anomaly becomes reachable through the tooling
itself. Rule 2's trigger is only safe while "no job row" cannot be
manufactured by deletion. Rule 3's state itself (succeeded job, pending
step) is likewise only reachable outside the engine — manual ad-hoc SQL, a
backup restore with mismatched table ages, or a misfiring retention job —
and the decision is **manual intervention only**: replay raises
`NotReplayable` for those jobs, and no automated repair command exists,
because the engine cannot know whether the step's effect happened.

### Workflow changes while running: snapshot BOTH delay and budget

The step list is snapshotted per instance in `workflow_steps` — names,
positions, `delay_seconds` AND `max_attempts` (migration 0004). The advance
transaction reads both from the snapshot, so a deploy changes neither the
timers nor the retry budgets of an in-flight workflow; only new instances
pick them up. Step *functions* are still resolved by name from the registry
at run time: renaming or removing a step a running instance has not reached
yet fails loudly when that step's job runs (registry lookup → job fails →
retries → `dead` → reconciler fails the workflow → DLQ replay after the fix),
never a silent skip. The advance transaction itself is registry-independent.

### Resume

If a claimed step's row is already `succeeded` (unreachable through the engine
under the atomic transaction; possible after manual repair), the handler skips
the step function but **still runs the advance transaction**: complete the
carrying job and ensure the next step's job exists (or mark the workflow
succeeded). Skipping the function alone would leave the redelivered job
uncompleted.

## Options considered

- **Separate workflow engine / own scheduler**: re-implements claiming,
  leases, retries, recovery; duplicates every Phase 1 bug class. Rejected.
- **Saga / orchestration framework** (Temporal et al.): correct but a large
  operational dependency; defeats the single-Postgres design goal. Rejected.
- **Two transactions (output save, then job complete)**: the complete-first
  crash is unrecoverable (above); the output-first variant needs the resume
  path to be a *reliability* requirement rather than a defense. Rejected in
  favor of one transaction.

## Consequences

### Gained

- Crash recovery for free: a crashed step's job is requeued by the reaper and
  re-enters at the same step (resume skips completed steps).
- Exactly one job per step by construction (dedupe key), which makes the
  reconciler's "missing job" repair safe.
- Timers are just `run_at`: durable delays with no blocking worker.
- One datastore, one recovery story, one set of tests.

### Given up

- Steps are strictly sequential (no fan-out/parallel steps in v1).
- Step functions must be idempotent (at-least-once) and JSON-only outputs.
- Workflow definitions are snapshotted per instance — delays AND retry
  budgets; changing either for a running workflow requires DLQ replay or a
  new instance.
- Every workflow state change costs queue row updates (bloat managed as in
  Phase 1: autovacuum tuning, retention).

### Fencing and replay: `attempts` is monotonic, claim-owned

`attempts` only moves forward: `claim` increments it; nothing else writes it.
Replay therefore **never rewinds it** (rewinding inverts stale-vs-current
under ordinal comparisons, conflates eras in `(job_id, attempt)`-keyed state,
and falsifies history). Replay gives fresh attempts by extending the budget:
`max_attempts = attempts + <budget>`, `replay_count += 1`, `state = 'queued'`,
`run_at = now()`, step back to `pending`, workflow back to `running`.

## When to revisit

Parallel steps / fan-out, sub-workflows, or cross-workflow barriers — if real
use cases appear — would extend the payload contract and the advance
transaction, not replace it.
