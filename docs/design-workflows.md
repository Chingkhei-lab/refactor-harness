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
workflow_steps   (workflow_id, position) PK, name, state, output, delay_seconds
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
  running" below): names, positions and `delay_seconds` are frozen at
  `start_workflow`.

## start_workflow (atomic)

One transaction on the (autocommit) connection via `with conn.transaction():`:

1. `INSERT INTO workflows ... ON CONFLICT (dedupe_key) ... DO UPDATE` — the
   same pattern as `jobs.enqueue`; `xmax = 0` in `RETURNING` distinguishes a
   fresh insert from the duplicate path.
2. On a fresh insert: bulk-insert one `workflow_steps` row per step
   (pending, `delay_seconds` from the definition).
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
   (`run_at = now() + next.delay_seconds`, `max_attempts` from the registry,
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

## Failure policy

- Step failures reuse the Phase 1 path: `fail()` with full-jitter backoff,
  `dead` at the **step's** `max_attempts`. A retry re-runs the step function
  (its effect deduplication is the step author's job, via `effect_key`).
- `NonRetryableError` (checkpoint 5) will send the job straight to `dead` via a
  `force_dead` parameter in `fail.sql`.
- A workflow goes `failed` when its **current** step's job is `dead`: the
  reconciler (checkpoint 5) records `failed_step` and `last_error`, and marks
  the step row `failed` in the same statement order.
- A dead step job is replayable through the DLQ (below).

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

## Reconciler (checkpoint 5, design)

A repair loop next to the reaper, one SQL statement per rule, **lock order
job → step → workflow** everywhere:

1. **Dead current step → workflow failed.** For each `running` workflow, its
   current step is the first non-succeeded step; if that step's job is `dead`,
   mark the step `failed` and the workflow `failed` with `failed_step` +
   `last_error`.
2. **Missing job → re-enqueue.** If the current step has **no job row at all**
   (not "no live job"), enqueue it — the dedupe key makes this safe. The
   narrower "no live job" trigger is explicitly wrong: a *succeeded* job row
   exists but isn't live, and re-enqueueing it returns the same succeeded job
   forever, so the repair would loop without doing anything.

**Is `succeeded job + pending step` reachable?** Under the atomic advance
transaction, no: a job can only flip to `succeeded` inside the same transaction
that marks its step `succeeded`. The state can only appear through manual SQL
or a future bug — exactly the state answer (c) called unrecoverable. The
reconciler therefore treats it as an anomaly to **surface, not repair**: mark
the workflow `failed` with `last_error` explaining the inconsistency (a test
will pin that it does not loop on it).

## Durable timers (checkpoint 5)

`delay_seconds` lives in the next step's job `run_at`
(`run_at = now() + delay`), set inside the advance transaction. No worker
blocks; the delay survives worker death because it is database state. The test
kills all workers during a delay and restarts them: the step must not run
early and must run promptly once `run_at` passes.

## Dead-letter view and replay (checkpoint 5)

`dead_letters` view: dead jobs joined with their workflow/step context.
`python -m engine.dlq list | replay <job_id>`. Replay, in one transaction
(lock order job → step → workflow):

- refuses non-dead jobs;
- `state = 'queued'`, `run_at = now()`, `replay_count += 1`;
- **never touches `attempts`** — see the note below;
- gives fresh attempts by **extending the budget**:
  `max_attempts = attempts + <budget>`;
- sets the step back to `pending` and the workflow back to `running`.

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
