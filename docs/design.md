# Design: Job Engine

## Goals

- Reliable **at-least-once** execution of background jobs.
- Crash-safe recovery: a job is never silently lost because a worker died.
- Bounded retries with exponential full-jitter backoff, then a terminal `dead` state.
- Duplicate enqueues with the same `dedupe_key` are safe and return the existing job.
- Simple, Postgres-backed queue (no external broker) using row-level locking.
- Observable state: every job has a clear lifecycle `state` and attempt count.

## Non-goals

- Exactly-once execution semantics (callers must be idempotent).
- Distributed scheduling / priority queues / fairness across tenants (initial version).
- Cross-region or multi-database replication.
- Real-time / low-latency dispatch (polling-based is acceptable).

## Column naming (canonical)

Use one set everywhere: `state`, `run_at`, `attempts`, `max_attempts`,
`locked_by`, `lease_expires_at`, `last_heartbeat_at`, `result`, `last_error`,
`dedupe_key`, `created_at`, `updated_at`. All lease and claim decisions use the
database's `now()`, never a worker's clock.

## Job state machine

```
                +-----------+
                |  queued   |
                +-----------+
                      |
                      | claim (SKIP LOCKED), attempts += 1
                      v
                +-----------+
                |  running  |
                +-----------+
                 /    |     \
   success     /     |      \  failure
              v      |       v
        +-----------+ |  +-----------+
        | succeeded | |  |  queued   |  (retry if attempts < max, backoff)
        +-----------+ |  +-----------+
                      |
                      | failure or lease expiry when attempts >= max
                      v
                +-----------+
                |   dead    |
                +-----------+
```

Transitions:

- `queued → running`: worker claims the job with
  `SELECT ... FOR UPDATE SKIP LOCKED`, filtered to job types the worker has
  handlers for (`type = ANY(...)`) so an unknown type never burns an attempt;
  sets `locked_by`, `lease_expires_at = now() + lease`, `last_heartbeat_at =
  now()`, and increments `attempts`.
- `running → succeeded`: handler returns without error; stores `result`. The
  write is guarded by `id`, `locked_by`, and `attempts`, and clears the lease
  columns.
- `running → queued`: a handler failure while `attempts < max_attempts`, or a
  lease expiry while `attempts < max_attempts`; `run_at` is set to
  `now() + backoff` (exponential with full jitter). Leaving `running` clears
  `locked_by`, `lease_expires_at`, and `last_heartbeat_at`.
- `running → dead`: a handler failure or lease expiry when
  `attempts >= max_attempts`; stores `last_error`. A dead job keeps its old
  `run_at` — no new `run_at` is computed for `dead`.
- `dead` is terminal; re-enqueue creates a new job (or resets via explicit replay).

**Lease expiry follows the attempts rule.** A job that crashes workers every
time increments `attempts` at each claim, so it eventually transitions to `dead`
instead of looping forever.

**Lease token.** The lease is identified by `(locked_by, attempts)`:

- `locked_by` is unique per worker process: `hostname + pid + random suffix`.
- `attempts` acts as the **fencing token**. It increments at claim time, so a
  stale worker from an earlier attempt carries an older `attempts` value; its
  heartbeat and completion writes fail the
  `WHERE id = ? AND locked_by = ? AND attempts = ?` guard. This prevents a
  delayed worker from overwriting a newer claim's result.

**Lease loss is loud.** `heartbeat`, `complete`, and `fail` all carry the same
`WHERE id AND locked_by AND attempts AND state = 'running'` guard; a write that
matches zero rows raises `LeaseLost`, which callers must not swallow. The
heartbeat thread runs every `lease / 3` on its own connection, sets a flag, and
exits when it loses the lease.

**Expiry is not revocation.** A lease that has expired but not yet been reaped
does not stop the original worker from completing its job: the row guard
(`locked_by`, `attempts`) decides, not the clock. The reaper reclaims; it does
not forbid late completion. If a new attempt claims the row first, the same
guard rejects the late write.

## Failure model

| Scenario | Detection | Recovery | Residual risk |
|---|---|---|---|
| Worker crashes mid-job | Lease expires (`running` job with stale `lease_expires_at`) | Reaper requeues as `queued` with backoff if `attempts < max`, else `dead`; attempt already counted | Partial side effects; callers must be idempotent |
| Worker freezes, wakes later | Heartbeat gap exceeds lease; another worker may have requeued and completed the job | On wake, the fenced write fails the lease guard; job already succeeded elsewhere | Duplicate execution possible if side effects non-idempotent |
| Database restarts | Connection errors; in-flight transactions roll back | Jobs remain in last committed state; expired-lease `running` jobs requeued by reaper on reconnect | None beyond retry semantics |
| Same job enqueued twice | `dedupe_key` / partial unique index | Second enqueue returns the existing job | None if key provided; without key, two independent jobs run (at-least-once) |
| Commit succeeded but ack lost (connection dropped after claim) | Job stays `running`; worker never receives acknowledgment | Lease expiry recovers it (requeue or dead per attempts rule) | Duplicate execution possible; idempotent handlers required |
| Heartbeat thread starved while handler alive | False lease loss; another worker reclaims the job | Duplicate execution of the job | Mitigation: generous lease relative to handler runtime + idempotent handlers |
| Worker clock skew | Inconsistent lease expiry decisions | Mitigation: use the database's `now()` for all lease/heartbeat logic | None if DB time is authoritative |
| Frozen worker row (stale worker keeps running) | Heartbeat thread detects lease loss | Cannot safely kill the handler; side effects may still duplicate | Mitigation: idempotent handlers; detect loss early via heartbeat thread |

## Dedupe semantics

Decision: **enqueueing the same `dedupe_key` returns the existing job for as
long as that row exists (no expiry window).** Re-running requires a new key or
an explicit replay/reset.

Two documented behaviors of this design, both accepted:

- The no-op `DO UPDATE` writes a new row version on every duplicate enqueue — a
  small, bounded cost that keeps enqueue a single statement.
- A duplicate enqueue ignores a different payload: the **first payload wins**.
  Callers that need different payloads must use a new key.

Implementation: partial unique index, and `ON CONFLICT` must repeat the index
predicate:

```sql
CREATE UNIQUE INDEX jobs_dedupe_key_uq
  ON jobs (dedupe_key)
  WHERE dedupe_key IS NOT NULL;

INSERT INTO jobs (...) VALUES (...)
ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL
DO UPDATE SET updated_at = jobs.updated_at
RETURNING ...;
```

## Worker model (Phase 1)

- Worker loop: reap if due → claim with `types = registry keys` → idle-wait on
  the stop event with jitter. `worker_id` is generated once per process:
  `hostname + pid + random suffix`.
- Handler contract: `handler(payload, ctx)`; `ctx` carries `job_id`, `attempt`,
  and a `lease_lost` event the handler can poll to cooperate with cancellation.
- One **long-lived heartbeat thread per worker** owns a single connection and
  beats the current job's token every `lease / 3` — beats land at `L/3`,
  `2L/3` and `L`, so **two** missed beats are tolerated (with interval `= L`,
  a single hiccup would expire the lease). Transient `OperationalError`s are
  retried after a reconnect: they are not lease loss. The thread tracks
  `last_successful_beat + lease` on the monotonic clock and only sets the
  lease-lost event when that deadline passes, because by then the reaper may
  legitimately have taken the job. Per job, the worker clears the token (and
  waits out any in-flight beat) **before** calling `complete`/`fail`, so no
  late beat can hit the cleared row.
- A reaper runs from each worker every few seconds. One statement: a CTE
  selects up to `batch` expired running jobs (`FOR UPDATE SKIP LOCKED`) and
  the outer `UPDATE ... FROM` applies the same requeue-or-dead CASE as
  `fail()`, repeating `state = 'running' AND lease_expires_at < now()` as a
  second guard (READ COMMITTED re-checks it when the row changed while the
  statement waited). It does **not** increment `attempts`, sets
  `last_error = 'lease expired'`, and clears the lease columns. Backoff
  decision: **requeue at `now()`** — the expired lease already acted as the
  delay, so SQL `random()` would only duplicate the Python full-jitter logic;
  dead jobs keep their old `run_at`, consistent with `fail()`. Accepted
  trade-off: a job that hard-crashes its worker (a poison job) never reaches
  `fail()`, so it burns one worker process per attempt, up to `max_attempts`,
  paced only by the lease duration — no jittered backoff ever slows it down.
  That is bounded (`max_attempts` caps the blast radius, each cycle still pays
  ~lease) and rare, while infra kills — the common case — recover with zero
  extra delay; everything that reaches `fail()` (handler exception,
  non-serializable or jsonb-rejected result) still gets full-jitter backoff.
- `fail()` requeues with `run_at = now() + backoff` while
  `attempts < max_attempts`, otherwise marks the job `dead`. Backoff is
  exponential with full jitter: uniform random between `0` and
  `min(cap, base * 2^attempts)`.
- SIGTERM: set the stop event only; the current job finishes, then the worker
  exits 0. The signal handler is registered in the main thread.
- Delivery is **at-least-once**; handlers must be idempotent (Phase 2 adds
  side-effect deduplication and a `NonRetryableError` that skips retries).

## Known pitfalls (observed) and operational notes

- **Splitting the claim broke exactly-once claiming.** Replacing the single
  claim statement with a plain `SELECT` + `UPDATE` (two autocommit statements)
  let 8 workers claim 1,000 jobs **4,129 times** — every worker re-claimed rows
  it had already seen because no lock survives between the two statements. The
  single statement with `FOR UPDATE SKIP LOCKED` is load-bearing.
- **Full jitter can land near zero.** A claim issued immediately after `fail()`
  can see `run_at` a few microseconds in the future (CI caught this flake).
  Workers must poll until `run_at` passes instead of assuming instant
  re-eligibility; the retry tests use the same polling.
- **The claim plan depends on fresh statistics.** On a freshly loaded table
  (10k queued + 100k succeeded) with no stats, the planner chose a bitmap scan
  + sort (~21 ms); after `ANALYZE jobs` it returned to an index scan
  (~0.54 ms). Run `ANALYZE` after bulk loads.
- **The `type = ANY(...)` claim filter skips past unreadable head-of-queue
  jobs.** If one worker's types sit behind many other types, the index scan
  walks those rows every claim. If that becomes a measurable problem, add
  `CREATE INDEX ... ON jobs (type, run_at, id) WHERE state = 'queued'`.
- **Queue tables bloat.** `jobs` is updated constantly; future work: tune
  autovacuum for `jobs`, and add a retention job that deletes old `succeeded`
  rows.
- **Phase 3 adds two more growth sources, and neither has a collector yet.**
  (a) Content-addressed **snapshot and patch blobs** plus the `workspaces` /
  `workspace_patches` references to them; (b) **terminal workflows**
  (`succeeded` / `failed`) and their step rows, whose workspaces and blobs are
  then unreachable. The rule is the ADR 002 retention rule mirrored for this
  phase: **never delete anything a non-terminal workflow references**, and a
  terminal workflow is never retried — that is exactly what makes collecting it
  safe. Install images (`engine-deps:<install_key>`) belong to the same story:
  one image per requirements × base digest, never pruned. Full decision record:
  ADR [`adr/003-sandbox.md`](adr/003-sandbox.md) "Future work — garbage
  collection"; measurements beside
  [`results/sandbox-2026-11-18.md`](results/sandbox-2026-11-18.md).
