-- Reconciler rule 2, statement 1/1: running workflows whose current (first
-- non-succeeded) step is pending but has NO job row at all — not "no live
-- job": a succeeded job row exists but is not live, and re-enqueueing it
-- would return the same succeeded job forever (rule 3's anomaly instead).
-- The repair re-enqueues the step's job with the snapshot's delay_seconds
-- and max_attempts (the second statement is jobs.enqueue in Python; the
-- dedupe key makes it idempotent). There is no job row to lock, so the step
-- rows are locked first and the workflow row is not touched at all (the
-- workflow stays running). SKIP LOCKED avoids colliding with the advance
-- transaction, which never locks the NEXT step's row.
SELECT
    s.workflow_id,
    s.position,
    s.name,
    s.delay_seconds,
    s.max_attempts
FROM workflow_steps s
JOIN workflows wf ON wf.id = s.workflow_id
WHERE wf.state = 'running'
  AND s.state = 'pending'
  AND s.position = (
      SELECT min(s2.position) FROM workflow_steps s2
      WHERE s2.workflow_id = wf.id AND s2.state <> 'succeeded'
  )
  AND NOT EXISTS (
      SELECT 1 FROM jobs j
      WHERE j.dedupe_key = 'wf:' || s.workflow_id::text
                         || ':step:' || s.position::text
  )
ORDER BY s.workflow_id, s.position
LIMIT %(batch)s
FOR UPDATE OF s
SKIP LOCKED;
