-- Reconciler rule 1, statement 2/3: mark the dead steps failed. The job rows
-- are already locked by statement 1 (global order: job, then step). The
-- guard is state <> 'succeeded' so a manually-created failed step is
-- re-marked (a no-op write) and statement 3 can still fail a stuck workflow.
UPDATE workflow_steps s
SET state = 'failed'
FROM jobs j
WHERE j.id = ANY(%(job_ids)s::uuid[])
  AND s.workflow_id = (j.payload ->> 'workflow_id')::uuid
  AND s.position = (j.payload ->> 'position')::int
  AND s.state <> 'succeeded'
RETURNING s.workflow_id, s.position;
