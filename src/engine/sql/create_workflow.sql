-- create_workflow: insert the workflow row; a duplicate dedupe_key returns it.
-- Same pattern as jobs enqueue: the no-op DO UPDATE makes the existing row the
-- RETURNING row. (xmax = 0) distinguishes a fresh insert from the conflict
-- path, so start_workflow skips creating steps/jobs for an existing workflow.
INSERT INTO workflows (type, input, state, dedupe_key)
VALUES (%(wf_type)s, %(input)s, 'running', %(dedupe_key)s)
ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL
DO UPDATE SET updated_at = workflows.updated_at
RETURNING
    id, type, input, state, dedupe_key, failed_step, last_error,
    created_at, updated_at,
    (xmax = 0) AS inserted;
