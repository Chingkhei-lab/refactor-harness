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
  `SELECT ... FOR UPDATE SKIP LOCKED`; sets `locked_by`,
  `lease_expires_at = now() + lease`, `last_heartbeat_at = now()`, and
  increments `attempts`.
- `running → succeeded`: handler returns without error; stores `result`. The
  write is guarded by `id`, `locked_by`, and `attempts`.
- `running → queued`: a handler failure while `attempts < max_attempts`, or a
  lease expiry while `attempts < max_attempts`; `run_at` is set to
  `now() + backoff` (exponential with full jitter).
- `running → dead`: a handler failure or lease expiry when
  `attempts >= max_attempts`; stores `last_error`.
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

- Worker loop: claim → run the handler from a registry (`dict[type, callable]`)
  → complete or fail. Idle workers sleep with jitter.
- Heartbeat runs in a separate thread every `lease / 3` on its **own
  connection**; a fenced update returning 0 rows means the lease is lost and the
  worker must stop without writing a result.
- A reaper runs from each worker every few seconds, reclaiming expired leases
  with `SKIP LOCKED`; it applies the same requeue-or-dead rule as `fail()` and
  sets `last_error = 'lease expired'`. It does **not** increment `attempts`
  (already counted at claim time).
- `fail()` requeues with `run_at = now() + backoff` while
  `attempts < max_attempts`, otherwise marks the job `dead`. Backoff is
  exponential with full jitter: uniform random between `0` and
  `min(cap, base * 2^attempts)`.
- SIGTERM: finish the current job, then exit.
- Delivery is **at-least-once**; handlers must be idempotent (Phase 2 adds
  side-effect deduplication and a `NonRetryableError` that skips retries).
