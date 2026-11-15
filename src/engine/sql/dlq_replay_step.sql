-- Replay, statement 3/4 (workflow_step jobs only): the step goes back to
-- pending so the replayed job can re-run it. Locked after the job row,
-- before the workflow row (global order job -> step -> workflow).
UPDATE workflow_steps
SET state = 'pending'
WHERE workflow_id = %(workflow_id)s
  AND position = %(position)s
RETURNING workflow_id, position, name;
