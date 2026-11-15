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
advance transaction, the reconciler, and replay all follow it; `DeadlockDetected`
is caught and retried once.

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
- Workflow definitions are snapshotted per instance; changing a running
  workflow's step list requires replay or a new instance.
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
