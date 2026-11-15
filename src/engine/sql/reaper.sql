-- reaper: reclaim expired leases in one statement.
--
-- The CTE selects up to batch expired running jobs, oldest lease first, with
-- FOR UPDATE SKIP LOCKED so two reapers never collide on the same rows. The
-- outer UPDATE ... FROM requeues or deads them with the same CASE as fail(),
-- and repeats state = 'running' AND lease_expires_at < now() as a second
-- guard: READ COMMITTED re-checks it when a row changed while the statement
-- waited, so a late-but-alive worker's committed heartbeat keeps its job.
--
-- Backoff decision: requeue at now(). The expired lease already acted as the
-- delay, so SQL random() would only duplicate the Python full-jitter logic
-- without adding anything. A dead job keeps its old run_at, consistent with
-- fail(). attempts is NOT incremented here: claim already did.
-- Lease columns are cleared because every row leaves 'running'.
WITH expired AS (
    SELECT id
    FROM jobs
    WHERE state = 'running'
      AND lease_expires_at < now()
    ORDER BY lease_expires_at
    LIMIT %(batch)s
    FOR UPDATE SKIP LOCKED
)
UPDATE jobs j
SET state = CASE
        WHEN j.attempts >= j.max_attempts THEN 'dead'
        ELSE 'queued'
    END,
    run_at = CASE
        WHEN j.attempts >= j.max_attempts THEN j.run_at
        ELSE now()
    END,
    last_error = 'lease expired',
    locked_by = NULL,
    lease_expires_at = NULL,
    last_heartbeat_at = NULL,
    updated_at = now()
FROM expired
WHERE j.id = expired.id
  AND j.state = 'running'
  AND j.lease_expires_at < now()
RETURNING
    j.id, j.type, j.payload, j.state, j.attempts, j.max_attempts, j.run_at,
    j.locked_by, j.lease_expires_at, j.last_heartbeat_at, j.result,
    j.last_error, j.dedupe_key, j.created_at, j.updated_at;
