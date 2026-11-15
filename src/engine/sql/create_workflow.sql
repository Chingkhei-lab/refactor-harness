-- create_workflow: insert the workflow row; a duplicate dedupe_key returns it.
-- Same pattern as jobs enqueue: the no-op DO UPDATE makes the existing row the
-- RETURNING row. Why (xmax = 0) instead of the documented alternative
-- (INSERT ... ON CONFLICT DO NOTHING RETURNING, then a SELECT when nothing
-- came back): one statement, one round trip, and the distinction is exact —
-- a row RETURNING from the plain INSERT path has xmax = 0 (only xmin is set
-- until something updates it), while the DO UPDATE path writes a new version
-- with xmax = our own xid. The dedupe tests below cover both paths.
INSERT INTO workflows (type, input, state, dedupe_key)
VALUES (%(wf_type)s, %(input)s, 'running', %(dedupe_key)s)
ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL
DO UPDATE SET updated_at = workflows.updated_at
RETURNING
    id, type, input, state, dedupe_key, failed_step, last_error,
    created_at, updated_at,
    (xmax = 0) AS inserted;
