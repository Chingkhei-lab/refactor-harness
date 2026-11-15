-- complete_step: mark a step succeeded with its output. Runs INSIDE the
-- advance transaction, after jobs.complete fenced the carrying job — the
-- lock order is job, then step, then workflow. The job's fenced complete is
-- what makes this idempotent: a stale attempt never reaches this statement.
UPDATE workflow_steps
SET state = 'succeeded',
    output = %(output)s
WHERE workflow_id = %(workflow_id)s
  AND position = %(position)s
RETURNING workflow_id, position, name, state, output, delay_seconds;
