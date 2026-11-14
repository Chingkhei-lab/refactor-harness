-- claim: one short statement, safe to run on an autocommit connection so no
-- transaction can be left open around the handler.
--
-- The subquery picks the next eligible job (queued, run_at <= now(), and a
-- type this worker can actually run, so an unknown type never burns an
-- attempt), ordered by (run_at, id), and takes FOR UPDATE SKIP LOCKED so
-- concurrent workers never queue behind each other's row locks. The outer
-- UPDATE flips it to running and sets the lease in the same statement;
-- attempts increments here, which is what makes it the fencing token.
--
-- The WHERE uses a scalar subquery (= SELECT ... LIMIT 1), which is evaluated
-- once. IN (SELECT ... LIMIT 1) is a known pitfall: the planner may execute
-- the subquery more than once (for example on the inner side of a nested
-- loop), and with SKIP LOCKED each re-run can lock a different row, so one
-- statement could claim more than one job.
UPDATE jobs
SET state = 'running',
    locked_by = %(locked_by)s,
    lease_expires_at = now() + make_interval(secs => %(lease_seconds)s),
    last_heartbeat_at = now(),
    attempts = attempts + 1,
    updated_at = now()
WHERE id = (
    SELECT id
    FROM jobs
    WHERE state = 'queued'
      AND run_at <= now()
      AND type = ANY(%(types)s)
    ORDER BY run_at, id
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
RETURNING
    id, type, payload, state, attempts, max_attempts, run_at,
    locked_by, lease_expires_at, last_heartbeat_at, result, last_error,
    dedupe_key, created_at, updated_at;
