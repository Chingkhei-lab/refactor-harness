-- Reconciler rule 1, statement 1/3: lock dead workflow-step jobs whose step
-- is the current (first non-succeeded) step of a RUNNING workflow. SKIP
-- LOCKED skips jobs a concurrent DLQ replay already holds. Locking the JOB
-- rows first — before the step and workflow updates in the same transaction
-- — keeps every multi-row writer in one global order: job -> step ->
-- workflow (ADR 002). updated_at gives the batch a stable, oldest-first
-- order; rows skipped to a concurrent locker simply leave a smaller batch.
SELECT
    j.id,
    (j.payload ->> 'workflow_id')::uuid AS workflow_id,
    (j.payload ->> 'position')::int AS position,
    j.last_error
FROM jobs j
JOIN workflow_steps s
  ON s.workflow_id = (j.payload ->> 'workflow_id')::uuid
 AND s.position = (j.payload ->> 'position')::int
JOIN workflows wf ON wf.id = s.workflow_id
WHERE j.type = 'workflow_step'
  AND j.state = 'dead'
  AND wf.state = 'running'
  AND s.state <> 'succeeded'
  AND s.position = (
      SELECT min(s2.position) FROM workflow_steps s2
      WHERE s2.workflow_id = wf.id AND s2.state <> 'succeeded'
  )
ORDER BY j.updated_at
LIMIT %(batch)s
FOR UPDATE OF j
SKIP LOCKED;
