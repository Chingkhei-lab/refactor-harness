-- enqueue: insert a job; a duplicate dedupe_key returns the existing job.
-- ON CONFLICT must repeat the partial index predicate. The no-op update makes
-- the existing row the RETURNING row, so callers get the old job back.
INSERT INTO jobs (
    type, payload, state, attempts, max_attempts, run_at, dedupe_key
) VALUES (
    %(job_type)s,
    %(payload)s,
    'queued',
    0,
    %(max_attempts)s,
    COALESCE(%(run_at)s, now() + COALESCE(%(delay_seconds)s, 0) * interval '1 second'),
    %(dedupe_key)s
)
ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL
DO UPDATE SET updated_at = jobs.updated_at
RETURNING
    id, type, payload, state, attempts, max_attempts, run_at,
    locked_by, lease_expires_at, last_heartbeat_at, result, last_error,
    dedupe_key, created_at, updated_at;
