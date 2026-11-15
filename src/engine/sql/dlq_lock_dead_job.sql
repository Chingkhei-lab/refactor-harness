-- Replay, statement 1/4: take the dead job's row lock FIRST — the global
-- lock order is job -> step -> workflow (ADR 002). Deliberately NOT SKIP
-- LOCKED: a replay wants the row even if a reconciler holds it, so it waits;
-- the reconciler is the side that skips. The refusal decision happens in
-- Python after the lock: a row that exists but is not 'dead' raises
-- NotReplayable and the whole transaction rolls back, lock released.
SELECT id, type, payload, state, attempts, max_attempts, replay_count
FROM jobs
WHERE id = %(job_id)s
FOR UPDATE;
