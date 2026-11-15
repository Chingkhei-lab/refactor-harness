-- Replay, statement 4/4: the workflow restarts. failed_step is cleared;
-- last_error is kept as the history of why it died. Guard-free on purpose:
-- replaying a job whose workflow is still 'running' (a race the reconciler
-- lost) is a no-op update, not an error.
UPDATE workflows
SET state = 'running',
    failed_step = NULL,
    updated_at = now()
WHERE id = %(workflow_id)s
RETURNING id, state;
