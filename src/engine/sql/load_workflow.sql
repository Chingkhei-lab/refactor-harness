SELECT
    id, type, input, state, dedupe_key, failed_step, last_error,
    created_at, updated_at
FROM workflows
WHERE id = %(workflow_id)s;
