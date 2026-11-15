-- The dead-letter listing: straight from the dead_letters view (migration
-- 0005), newest first. Plain (non-workflow) dead jobs appear with NULL
-- workflow/step context — they are replay targets too.
SELECT
    id, type, workflow_id, step_position, step_name, workflow_type,
    workflow_state, attempts, max_attempts, replay_count, last_error,
    updated_at
FROM dead_letters
ORDER BY updated_at DESC
LIMIT %(limit)s;
