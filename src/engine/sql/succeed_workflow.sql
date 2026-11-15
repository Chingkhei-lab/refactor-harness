-- succeed_workflow: finish the workflow after its last step advanced.
-- Guarded on state = 'running' so a redelivered last-step job (resume path)
-- is a no-op rather than an error.
UPDATE workflows
SET state = 'succeeded',
    updated_at = now()
WHERE id = %(workflow_id)s
  AND state = 'running'
RETURNING
    id, type, input, state, dedupe_key, failed_step, last_error,
    created_at, updated_at;
