# Design: Durable Workflows (Phase 2)

Workflows built **on top of** the Phase 1 job engine. The full engine design is
in [`design.md`](design.md); the architecture decision is in
[`adr/002-workflows-on-jobs.md`](adr/002-workflows-on-jobs.md).

## Contract

- **Each step is one job.** A step executes as an ordinary Phase 1 job
  (`type = 'workflow_step'`), so it inherits claims, leases, fencing, heartbeats,
  the reaper, retries with full-jitter backoff, and `dead` at `max_attempts`.
- **Execution is at-least-once.** A step function may run more than once
  (crash mid-run, stale attempt racing). Effects are made idempotent by an
  `effect_key`; the engine never promises exactly-once execution.
- **Step outputs are JSON.** A step returns a dict (or None → `{}`); it is
  stored in `workflow_steps.output` (jsonb) and flows into later steps as
  `prior_outputs`. The same jsonb-hostility rules as job results apply (NaN,
  `\u0000` → the database rejects them → the job fails, not the worker).
- **Steps run in order, one at a time.** Step `position + 1`'s job is enqueued
  only by step `position`'s advance transaction. No parallel steps in v1.

## Data model (migration 0003)

```sql
workflows        id, type, input, state, dedupe_key, failed_step,
                 last_error, created_at, updated_at
workflow_steps   (workflow_id, position) PK, name, state, output,
                 delay_seconds, max_attempts
```

- `workflows.state`: `running | succeeded | failed` (terminal: succeeded/failed).
- `workflow_steps.state`: `pending | succeeded | failed`. There is no `running`
  step state: between claim and commit the carrying **job** is `running`, which
  is the authoritative "in flight" signal.
- A step's job carries `{workflow_id, position}` in its payload and
  `dedupe_key = 'wf:<workflow_id>:step:<position>'`. The Phase 1 dedupe rule
  (duplicate enqueue returns the existing job forever) guarantees **exactly one
  job row per step** — the reconciler and resume paths lean on this.
- **The `workflow_steps` rows are the snapshot** (see "Workflow changes while
  running" below): names, positions, `delay_seconds` AND `max_attempts` are
  frozen at `start_workflow` — a deploy changes neither the delays nor the
  retry budgets of an in-flight workflow.

## start_workflow (atomic)

One transaction on the (autocommit) connection via `with conn.transaction():`:

1. `INSERT INTO workflows ... ON CONFLICT (dedupe_key) ... DO UPDATE` — the
   same pattern as `jobs.enqueue`; `xmax = 0` in `RETURNING` distinguishes a
   fresh insert from the duplicate path.
2. On a fresh insert: bulk-insert one `workflow_steps` row per step
   (pending, `delay_seconds` and `max_attempts` from the definition).
3. Enqueue step 0's job with `run_at = now() + steps[0].delay_seconds` and
   `max_attempts = steps[0].max_attempts` (per-step retry budget).

A duplicate `dedupe_key` returns the existing workflow and creates **no extra
rows** (the steps/job statements are skipped). Like job payloads, the **first
input wins** for a duplicated key. No key → independent workflows.

## Step execution (the core)

Two phases in the `workflow_step` handler:

**Phase A — outside any transaction.** Load the workflow, its steps and the
definition from the registry (by step *name* — see snapshot rules). Resolve
`prior_outputs = {name: output for succeeded steps < position}`. Build
`StepContext` (`workflow_id`, workflow type, step name, attempt,
`effect_key = '<workflow_id>:<step_name>'`, `lease_lost`). Run
`fn(ctx, prior_outputs)` — plain Python, no transaction held, no locks held.
Register an `after_complete` callback that closes the step out.

**Phase B — one short transaction.** The worker wraps its `complete` and the
registered callbacks in `with conn.transaction():`. Inside, in this order:

1. `jobs.complete(...)` with the **full claim token** — the fence. Zero rows
   (`LeaseLost`) raises out of the block: the whole transaction rolls back and
   the output is discarded. A stale worker's step write dies with its lease.
2. `UPDATE workflow_steps SET state = 'succeeded', output = ...` for this
   step. (Lock order: **job → step → workflow** — see below.)
3. Either enqueue the next step's job
   (`run_at = now() + next.delay_seconds`, `max_attempts` from the snapshot,
   dedupe key makes it idempotent) or, after the last step,
   `UPDATE workflows SET state = 'succeeded'`.

**Why one transaction.** If the output save and the job complete were separate
transactions, the fatal ordering is complete-first: the job row says
`succeeded`, the step row still `pending`, the output lost — and nothing can
repair it, because the step's dedupe key returns the existing **succeeded** job
forever and DLQ replay refuses non-dead jobs. The workflow would be stuck in
`running` permanently. The safe ordering (output first, complete second) is
self-healing: the reaper requeues the still-`running` job and the resume path
skips the already-succeeded step. One transaction with complete **last under
the fence** gives both properties at once: commit the output, then mark done,
atomically — and a lost completion is recoverable while a lost output is
impossible.

**The `after_complete` hook.** `HandlerContext.after_complete(callback)` —
the worker runs callbacks inside the same transaction as `complete`. Contract:
the callback receives the worker's connection, uses **only** that connection,
and must not commit or open its own transaction. Any exception in the callback
rolls the transaction back (including the `complete`) and the job is failed —
it will retry and die loudly rather than advance half a step.

**Resume skips completed steps.** If the loaded step is already `succeeded`
(possible after manual repair or future non-atomic paths; unreachable through
the engine itself — see the reconciler note), the handler skips the step
function but **still runs the advance transaction**: it completes the carrying
job and ensures the next step's job exists (dedupe makes this idempotent) or
marks the workflow succeeded. Skipping the function alone would leave a
redelivered job uncompleted and the workflow stalled.

## Crash windows (the three review questions)

**Q1 — the connection drops during COMMIT of the advance transaction, so the
worker cannot tell whether it committed.** The database is in one of two
states, and both are safe:

1. *Rolled back.* The transaction was still uncommitted when the session died;
   Postgres aborts it. The job is still `running` (its lease belongs to the
   dead session and expires untouched), the step is still `pending`, the
   output is lost, and no next job was enqueued. Safe because the lease
   expiry makes the reaper requeue the job and the next attempt re-runs the
   step — at-least-once, the whole design assumption.
2. *Committed.* The commit hit the WAL and only the acknowledgement was lost.
   The job is `succeeded` with its result, the step is `succeeded` with its
   output, and the next job was enqueued — exactly the intended end state,
   atomically. Nothing to repair.

What the worker does in both cases: the same thing — nothing clever. The
`OperationalError` is caught, the connection is discarded
(`Worker._reconnect`), and the loop continues. It deliberately does NOT retry
the transaction blind (the next interaction with the database reveals
reality: state 1 comes back as a redelivered job after the lease expires;
state 2 simply never comes back). The claim token plus `(locked_by,
attempts)` fencing keep a reconnecting worker from racing whatever the reaper
did meanwhile.

**Q2 — what if the next job were enqueued AFTER the commit instead of
inside it?** Crash between COMMIT and the enqueue leaves: job `succeeded`,
step `succeeded`, output stored — but step k+1's job never exists, so the
workflow sits `running` forever with no work in flight. Which component
repairs it: **reconciler rule 2** (running workflow, current step pending,
NO job row for the dedupe key → re-enqueue). That rule exists precisely as
the safety net that would make the after-commit design survivable; with the
enqueue inside the transaction the window does not exist at all, and rule 2
covers manual damage in the same state shape.

**Q3 — a reconciler marks a workflow failed while an operator replays its
dead job.** No deadlock is possible because both sides take locks in the one
global order **job → step → workflow**: rule 1's first statement locks the
dead job rows (`FOR UPDATE ... SKIP LOCKED`), replay's first statement locks
the same row (`FOR UPDATE`, no skip — a single replay prefers to wait).
Same order, so at worst one waits for the other; a cycle needs someone to
hold a later lock while wanting an earlier one. Legal final states (both
operators complete, replay's one deadlock retry included):

- **replay applied** (first, or after the reconciler committed): workflow
  `running`, step `pending`, job `queued` — the reconciler's SKIP LOCKED
  simply means it saw nothing that cycle;
- **reconciler applied and replay gave up** (deadlocked twice): workflow
  `failed`, step `failed`, job `dead`.

Corrupt mixtures (a queued job on a failed workflow, a failed step on a
running one) would mean one side's transaction was torn — impossible, since
each side is one transaction. The race test (`test_replay_races_reconciler_
final_state_is_legal`) asserts exactly this tuple membership over 30 rounds.

## Failure policy

- Step failures reuse the Phase 1 path: `fail()` with full-jitter backoff,
  `dead` at the **step's** `max_attempts` (from the snapshot). A retry re-runs
  the step function (its effect deduplication is the step author's job, via
  `effect_key`).
- `NonRetryableError` (raised by a handler) sends the job straight to `dead`
  via a `force_dead` flag in `fail.sql` — `CASE WHEN force_dead OR
  attempts >= max_attempts THEN 'dead'`. Only the SQL decides the state; the
  worker passes the flag. The step's job is dead on the same attempt, with no
  backoff.
- A workflow goes `failed` when its **current** step's job is `dead`: the
  reconciler records `failed_step` and `last_error` (the job's own error) and
  marks the step row `failed`. **The worker does not mark the workflow on
  fail** — the reaper can kill a job with no worker involved, so one repair
  path owns the transition. The cost is latency: at most the reconciler
  cadence (`ENGINE_RECONCILER_INTERVAL_SECONDS`, default 5 s) plus the cycle
  time; chaos v2 (checkpoint 6) reports the observed delay. Recorded as the
  decision in ADR 002.- A dead step job is replayable through the DLQ (below).

## Reconciler (implemented)

A repair loop next to the reaper (each worker's maintenance cycle, on a
cadence and batch size from settings). Three rules, each a separate
transaction of separate statements, every statement taking row locks in the
global order **job → step → workflow**, each selection `SKIP LOCKED`:

1. **Dead current step → step + workflow failed.** Statement 1 locks the dead
   `workflow_step` jobs whose step is the workflow's current (first
   non-succeeded) step of a running workflow; statement 2 marks the steps
   `failed`; statement 3 marks the workflows `failed` with `failed_step` and
   the job's `last_error`. Separate statements instead of one
   multi-CTE UPDATE because PostgreSQL does not document the execution order
   of data-modifying CTEs; statements in one transaction have a guaranteed
   order and hold their locks until commit.
2. **Missing job → re-enqueue.** If the current step is pending and has **no
   job row at all** (not "no live job"), enqueue it with the snapshot's
   `delay_seconds` and `max_attempts` — the dedupe key makes it safe and
   idempotent. The narrower "no live job" trigger is explicitly wrong: a
   *succeeded* job row exists but isn't live, and re-enqueueing it returns
   the same succeeded job forever.
3. **Succeeded job + pending step → workflow failed, never re-enqueue.**
   Unreachable through the engine (the job can only flip succeeded inside
   the same transaction that marks its step succeeded) and unrecoverable —
   the step's dedupe key returns the succeeded job forever, and DLQ replay
   refuses non-dead jobs. The reconciler **surfaces** it: the workflow is
   marked `failed` with an explanatory `last_error`; a test pins that
   repeated cycles do not loop on it.
   How it can still appear in production: an ad-hoc manual `UPDATE`/`DELETE`
   by an operator or script, a restore from backups where `jobs` and
   `workflow_steps` were dumped at different moments, or the future retention
   job misfiring (see below). **Decision: no automated repair command.** The
   engine cannot know whether the step's effect actually happened — one of
   the two rows is simply wrong — so replay raises `NotReplayable` (the job
   is succeeded) and the operator investigates by hand: compare the effect
   records or the external system, then fix the rows manually, explicitly
   outside the engine. An automated "fix" would either re-execute an effect
   or fabricate a step success.

**Retention (future work) must never free live dedupe keys.** If retention
ever deletes old job rows, it must only delete jobs whose workflow is
terminal (`succeeded`/`failed`). Deleting a `workflow_step` job of a running
workflow frees the step's dedupe key: rule 2 would then re-enqueue it and
create a second job for a step that may have already run — the effect stays
idempotent, but the state machine now has two eras of the same step, and a
succeeded job under a pending step (rule 3) becomes reachable through the
tooling itself. Rule 2's "no job row at all" trigger is only safe while
"no job row" cannot be manufactured by deletion.

`DeadlockDetected` skips the rule (the transaction rolls back; the next
cycle retries it from scratch); the worker's hook tolerates one raised
between rules the same way.

## Workflow changes while running

**Decision: the step list is snapshotted per instance** in `workflow_steps`
(names, positions, delays frozen at `start_workflow`). Execution resolves step
*functions* by name from the registry at run time. Consequences:

- Adding steps to a definition affects only **new** instances.
- Renaming/removing a step a running instance hasn't reached yet: when its job
  is claimed, the registry lookup fails → the job fails loudly ("no step
  function named X") → retries → `dead` → reconciler fails the workflow → DLQ
  replay after the registry is fixed. Never a silent skip.
- Changing a step's *code* applies to whichever attempt runs next (normal
  deploy semantics).

## Durable timers (implemented)

`delay_seconds` lives in the next step's job `run_at`
(`run_at = now() + delay`), set inside the advance transaction. No worker
blocks; the delay survives worker death because it is database state. The
test runs a worker through step a, stops it mid-delay, and starts a fresh
worker: the step must not run early (the live worker cannot claim it while
`run_at` is future) and must run promptly once `run_at` passes.

## Dead-letter view and replay (implemented)

The `dead_letters` view (migration 0005) lists every dead job — plain jobs
with NULL workflow context, workflow steps joined to their step and workflow
rows — plus `replay_count`. `python -m engine.dlq list [--limit N]` reads it;
`python -m engine.dlq replay <job_id> [--budget N]` replays (budget defaults
to the configured `max_attempts`). Replay, in one transaction (lock order
job → step → workflow):

- locks the job `WHERE id = ? FOR UPDATE` and **refuses** (`NotReplayable`)
  anything missing or not `dead`;
- sets `state = 'queued'`, `run_at = now()`, `replay_count += 1`;
- **never touches `attempts`** — see the note below;
- gives fresh attempts by **extending the budget**:
  `max_attempts = attempts + <budget>`;
- sets the step back to `pending` and the workflow back to `running`,
  clearing `failed_step` (`last_error` is kept as history);
- retries the whole transaction exactly once on `DeadlockDetected`, then
  propagates.

**Replay has no cap** — each replay extends `max_attempts`, so a poison step
can be replayed forever. That is accepted for a manual operator command, and
`replay_count` is the human's visibility: the view exposes it and `dlq list`
prints `replayed=N` on every row. A job whose workflow is rule-3-failed
(succeeded job, pending step) is **not** replayable at all — see the
reconciler section for the manual path.

### Note: why replay must never rewind `attempts`

**What breaks if `attempts` goes backward.** `attempts` is the fencing token
precisely because it only moves forward (claim increments it; nothing else
writes it). Rewinding it:

1. **Inverts the token's meaning.** The row guards use equality
   (`attempts = ?`), so a rewind does not immediately break them — a stale
   worker's token still mismatches — but that safety becomes *accidental*
   (it now rests entirely on `locked_by` being unique per process). Any
   ordinal reasoning — "my attempt is older than the row's, so I'm stale" —
   inverts: after a rewind to 0, a stale era-1 attempt-5 worker *looks newer
   than the row* (5 > 0). One careless `>`/`>=` comparison in future code
   becomes a fencing hole.
2. **Conflates eras in attempt-keyed state.** `side_effects` keyed by
   `(job_id, attempt)`: after a rewind, the replay's attempt 1 reuses the dead
   era's `(job_id, 1)` key. `ON CONFLICT DO NOTHING` swallows the *new* effect,
   and the violation detector fires a false positive ("same (job_id, attempt)
   executed twice"). Either way the evidence is corrupted — you cannot tell
   which era produced a row, and "effects match attempts" checks pass for the
   wrong reason.
3. **Falsifies history.** `attempts` is the record of how many times the job
   really ran (and feeds backoff bounds and `max_attempts` decisions). Dead at
   5/5 → rewind to 0 → after replay it shows `attempts = 1` having run 6+ times.

**What replay does instead.** Leave `attempts` alone (only `claim` writes it)
and change what the budget is measured against:
`max_attempts = attempts + <budget>`, `replay_count += 1`. The token keeps
growing (next claim → attempts 6, 7, … — brand-new `(job_id, attempt)` keys),
the job gets a genuinely fresh retry budget, and the dead-era history stays
intact. (A zero-budget replay is the degenerate case: the next claim makes
`attempts ≥ max_attempts`, so the job runs once more and deads again on
failure.) Replay is also the explicit escape hatch from the Phase 1 dedupe
rule: the dead row is updated **in place** because re-enqueueing its dedupe
key would return the dead job forever.

## Idempotent effects (checkpoint 5 / chaos v2)

The chaos handler writes to an `effects` table with
`UNIQUE (effect_key) ... ON CONFLICT DO NOTHING`, `effect_key =
'<workflow_id>:<step_name>'` — one row per step execution *effect*, regardless
of attempts. Note for real integrations: an external call is only idempotent
if the **provider accepts an idempotency key**. If it doesn't, the effect and
its record cannot be made atomic and a crash in between duplicates the
external call — that is residual risk the caller accepts (mitigate with
provider-side dedupe or tolerant downstream checks).

## Lock order and deadlocks

Every multi-row writer takes row locks in one global order — **the carrying
job first, then the step row, then the workflow row** (the next-job INSERT is
last; inserts serialize on the step's unique dedupe index only against the
same key). The advance transaction, the reconciler and replay all follow it.
`DeadlockDetected` is caught and retried **once** (checkpoint 5, with a test
where replay races a step completion).

## Chaos v2 (checkpoint 6)

2,000 workflows × 4 steps (one with a 2 s delay), 3 workers, same kill loop;
2% injected always-failing steps, 1% non-retryable errors. Gate: every
workflow terminal; succeeded workflows have exactly one effect row per step;
outputs chain (running sum); failed == injected; one replayed workflow
finishes. Report duplicate executions (> 0, expected) vs duplicate effects
(0, target), attempts histogram, duration, jobs/s, kills.
