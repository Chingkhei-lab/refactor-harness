-- Reconciler rule 1, statement 3/3: fail the workflow, recording which step
-- died and the job's own last_error. Runs after the step update in the same
-- transaction (global order: job -> step -> workflow). The wf.state =
-- 'running' guard makes concurrent reconcilers idempotent: the loser waits
-- on the row lock, re-checks against the committed row, and no-ops.
UPDATE workflows wf
SET state = 'failed',
    failed_step = s.name,
    last_error = j.last_error,
    updated_at = now()
FROM jobs j
JOIN workflow_steps s
  ON s.workflow_id = (j.payload ->> 'workflow_id')::uuid
 AND s.position = (j.payload ->> 'position')::int
WHERE j.id = ANY(%(job_ids)s::uuid[])
  AND wf.id = s.workflow_id
  AND wf.state = 'running'
RETURNING wf.id;
