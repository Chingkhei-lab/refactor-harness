-- Reconciler rule 3: a SUCCEEDED job with a PENDING step is unreachable
-- through the engine (both flip inside the same advance transaction) and
-- unrecoverable by re-enqueue — the step's dedupe key returns the succeeded
-- job forever, and DLQ replay refuses non-dead jobs. So this anomaly is
-- surfaced, not repaired: fail the workflow with an explanatory error and
-- NEVER re-enqueue (a test pins that repeated cycles do not loop on it).
-- Only the workflow row is written; concurrent reconcilers serialize on the
-- wf.state = 'running' guard, and 'succeeded' is immutable, so the EXISTS
-- read cannot go stale.
WITH inconsistent AS (
    SELECT s.workflow_id, s.name
    FROM workflow_steps s
    JOIN workflows wf ON wf.id = s.workflow_id
    WHERE wf.state = 'running'
      AND s.state = 'pending'
      AND s.position = (
          SELECT min(s2.position) FROM workflow_steps s2
          WHERE s2.workflow_id = wf.id AND s2.state <> 'succeeded'
      )
      AND EXISTS (
          SELECT 1 FROM jobs j
          WHERE j.dedupe_key = 'wf:' || s.workflow_id::text
                                     || ':step:' || s.position::text
            AND j.state = 'succeeded'
      )
    LIMIT %(batch)s
)
UPDATE workflows wf
SET state = 'failed',
    failed_step = i.name,
    last_error = format(
        'inconsistent state: step %%s has a succeeded job but the step row'
        ' is pending; not repairable by re-enqueue (DLQ replay handles dead'
        ' jobs only); manual investigation required',
        i.name
    ),
    updated_at = now()
FROM inconsistent i
WHERE wf.id = i.workflow_id
  AND wf.state = 'running'
RETURNING wf.id;
