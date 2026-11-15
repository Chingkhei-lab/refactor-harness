SELECT
    workflow_id, position, name, state, output, delay_seconds
FROM workflow_steps
WHERE workflow_id = %(workflow_id)s
ORDER BY position;
