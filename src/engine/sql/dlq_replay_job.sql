-- Replay, statement 2/4: requeue the dead job IN PLACE (updating the row is
-- the explicit escape hatch from the Phase 1 dedupe rule: re-enqueueing its
-- dedupe key would return the dead job forever). `attempts` is untouched —
-- claim owns it and it only moves forward, keeping the fencing token and the
-- (job_id, attempt) effect keys meaningful (ADR 002). Fresh attempts come
-- from extending the budget: max_attempts = attempts + budget. replay_count
-- counts eras. last_error is kept as history. The state guard is redundant
-- with the Python check (the row is already locked) — kept as defense.
UPDATE jobs
SET state = 'queued',
    run_at = now(),
    max_attempts = attempts + %(budget)s,
    replay_count = replay_count + 1,
    updated_at = now()
WHERE id = %(job_id)s
  AND state = 'dead'
RETURNING id, state, attempts, max_attempts, replay_count;
