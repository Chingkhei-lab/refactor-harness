-- fail: one statement decides requeue-or-dead with a CASE on
-- attempts >= max_attempts, so the decision is made atomically in SQL.
-- - attempts < max: requeue with run_at = now() + backoff (jitter computed in
--   Python from the token's attempts, passed in as seconds).
-- - attempts >= max: dead, and a dead job keeps its old run_at (no new one).
-- Either way the job leaves 'running': clear locked_by, lease_expires_at and
-- last_heartbeat_at.
UPDATE jobs
SET state = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'queued' END,
    run_at = CASE
        WHEN attempts >= max_attempts THEN run_at
        ELSE now() + make_interval(secs => %(backoff_seconds)s)
    END,
    last_error = %(last_error)s,
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
