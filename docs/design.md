# Design: Job Engine

## Goals

- Reliable **at-least-once** execution of background jobs.
- Crash-safe recovery: a job is never silently lost because a worker died.
- Bounded retries with exponential backoff, then a terminal `dead` state.
- Idempotency-friendly: duplicate enqueues and redeliveries are handled gracefully.
- Simple, Postgres-backed queue (no external broker) using row-level locking.
- Observable state: every job has a clear lifecycle status and attempt count.

## Non-goals

- Exactly-once execution semantics (callers must be idempotent).
- Distributed scheduling / priority queues / fairness across tenants (initial version).
- Cross-region or multi-database replication.
- Real-time / low-latency dispatch (polling-based is acceptable).

## Column naming (canonical)

Use one set everywhere: `status`, `run_after`, `attempts`, `max_attempts`,
`locked_by`, `lease_expires_at`, `heartbeat_at`, `idempotency_key`, `queue`,
`last_error`, `created_at`, `started_at`, `finished_at`. All lease comparisons
use the database's `now()`.

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

- `queued → running`: worker claims job with `SELECT ... FOR UPDATE SKIP LOCKED`;
  sets `started_at`, increments `attempts`, sets `locked_by` and `lease_expires_at`.
- `running → succeeded`: handler returns without error; sets `finished_at`. Guarded
  by `status = running` and the owning lease token.
- `running → queued`: handler raises a retryable error, **or** the lease expires
  while `attempts < max_attempts`; `run_after` set with backoff.
- `running → dead`: non-retryable error, **or** lease expiry / failure when
  `attempts >= max_attempts`; sets `finished_at` and `last_error`.
- `dead` is terminal; re-enqueue creates a new job (or resets via explicit replay).

**Lease expiry follows the attempts rule.** A job that crashes workers every
time increments `attempts` at each claim, so it eventually transitions to `dead`
instead of looping forever.

**Lease token.** The lease is identified by `(locked_by, attempts)`:

- `locked_by` is unique per worker process: `hostname + pid + random suffix`.
- `attempts` acts as the **fencing token**. It increments at claim time, so a
  stale worker from an earlier attempt carries an older `attempts` value; its
  completion/heartbeat writes fail the `WHERE locked_by = ? AND attempts = ?`
  guard. This prevents a delayed worker from overwriting a newer claim's result.

## Failure model

| Scenario | Detection | Recovery | Residual risk |
|---|---|---|---|
| Worker crashes mid-job | Lease expires (`running` job with stale `heartbeat_at`) | Sweeper requeues as `queued` with backoff if `attempts < max`, else `dead`; attempt already counted | Partial side effects; callers must be idempotent |
| Worker freezes, wakes later | Heartbeat gap exceeds lease; another worker may have requeued and completed the job | On wake, completion write fails lease guard; job already succeeded elsewhere | Duplicate execution possible if side effects non-idempotent |
| Database restarts | Connection errors; in-flight transactions roll back | Jobs remain in last committed state; expired-lease `running` jobs requeued by sweeper on reconnect | None beyond retry semantics |
| Same job enqueued twice | Idempotency key / partial unique index on `(queue, idempotency_key)` | Second enqueue returns the existing job | None if key provided; without key, two independent jobs run (at-least-once) |
| Commit succeeded but ack lost (connection dropped after claim) | Job stays `running`; worker never receives acknowledgment | Lease expiry recovers it (requeue or dead per attempts rule) | Duplicate execution possible; idempotent handlers required |
| Heartbeat thread starved while handler alive | False lease loss; another worker reclaims the job | Duplicate execution of the job | Mitigation: generous lease relative to handler runtime + idempotent handlers |
| Worker clock skew | Inconsistent lease expiry decisions | Mitigation: use the database's `now()` for all lease/heartbeat logic | None if DB time is authoritative |
| Frozen worker row (stale worker keeps running) | Heartbeat thread detects lease loss | Cannot safely kill the handler; side effects may still duplicate | Mitigation: idempotent handlers; detect loss early via heartbeat thread |

## Dedupe semantics

Decision: **enqueueing a key that already succeeded returns the existing (succeeded)
job indefinitely (no expiry window).** A job with a given idempotency key is
deduplicated forever; re-running requires a new key or an explicit replay/reset.

Implementation: partial unique index

```sql
CREATE UNIQUE INDEX jobs_queue_idempotency_key_uq
  ON jobs (queue, idempotency_key)
  WHERE idempotency_key IS NOT NULL;
```

`ON CONFLICT` must repeat the index predicate, e.g.

```sql
INSERT INTO jobs (...) VALUES (...)
ON CONFLICT (queue, idempotency_key) WHERE idempotency_key IS NOT NULL
DO NOTHING;
```

## Notes

- At-least-once delivery implies handlers must be idempotent or fenced by external state.
- Leases and heartbeats are the single mechanism for crash/freeze detection; keep leases generous relative to job runtime.
- Dead jobs require manual replay or an admin operation; they are never retried automatically.
- All time comparisons use the database's `now()` to avoid worker clock skew.
