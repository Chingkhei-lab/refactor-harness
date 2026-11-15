-- create_workflow_steps: bulk-insert the per-instance snapshot of the step
-- list. Names, positions, delay_seconds AND max_attempts are frozen at
-- start_workflow; step functions are resolved by name from the registry at
-- run time. A deploy therefore changes delays and retry budgets for new
-- instances only (ADR 002).
INSERT INTO workflow_steps (workflow_id, position, name, state, delay_seconds, max_attempts)
SELECT %(workflow_id)s, pos, name, 'pending', delay, budget
FROM unnest(
    %(positions)s::int[],
    %(names)s::text[],
    %(delays)s::float8[],
    %(budgets)s::int[]
) AS t(pos, name, delay, budget);
