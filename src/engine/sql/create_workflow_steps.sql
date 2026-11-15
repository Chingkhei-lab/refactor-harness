-- create_workflow_steps: bulk-insert the per-instance snapshot of the step
-- list. Names, positions and delay_seconds are frozen at start_workflow;
-- step functions are resolved by name from the registry at run time.
INSERT INTO workflow_steps (workflow_id, position, name, state, delay_seconds)
SELECT %(workflow_id)s, pos, name, 'pending', delay
FROM unnest(
    %(positions)s::int[],
    %(names)s::text[],
    %(delays)s::float8[]
) AS t(pos, name, delay);
