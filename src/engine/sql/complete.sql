-- complete: mark succeeded and store the result, fenced by the claim token.
-- A worker that froze, got reclaimed, and woke up carries a stale attempts
-- value, so its write matches zero rows and cannot overwrite the newer
-- attempt's result. Leaving 'running' clears the lease columns.
UPDATE jobs
SET state = 'succeeded',
    result = %(result)s,
    locked_by = NULL,
    lease_expires_at = NULL,
    last_heartbeat_at = NULL,
    updated_at = now()
WHERE id = %(job_id)s
  AND locked_by = %(locked_by)s
  AND attempts = %(attempts)s
  AND state = 'running'
RETURNING
    id, type, payload, state, attempts, max_attempts, run_at,
    locked_by, lease_expires_at, last_heartbeat_at, result, last_error,
    dedupe_key, created_at, updated_at;
