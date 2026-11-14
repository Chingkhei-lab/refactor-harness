-- heartbeat: extend the lease and stamp last_heartbeat_at.
-- The WHERE clause is the fence: id + locked_by + attempts + state = 'running'.
-- Zero rows updated means the lease was lost (expired and reclaimed, or the
-- job left running); the caller must raise LeaseLost, never swallow it.
UPDATE jobs
SET lease_expires_at = now() + make_interval(secs => %(lease_seconds)s),
    last_heartbeat_at = now(),
    updated_at = now()
WHERE id = %(job_id)s
  AND locked_by = %(locked_by)s
  AND attempts = %(attempts)s
  AND state = 'running'
RETURNING
    id, type, payload, state, attempts, max_attempts, run_at,
    locked_by, lease_expires_at, last_heartbeat_at, result, last_error,
    dedupe_key, created_at, updated_at;
