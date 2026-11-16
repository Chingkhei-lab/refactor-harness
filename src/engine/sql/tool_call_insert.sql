-- One row per executed tool call. The UNIQUE (job_id, attempt, call_index)
-- is the dedupe: a retried step re-executes its calls from call_index 0 and
-- ON CONFLICT DO NOTHING keeps the first record — no duplicate rows.
INSERT INTO tool_calls
    (job_id, attempt, call_index, tool, args_json, ok, error_code,
     error_message, duration_ms, output_bytes)
VALUES
    (%(job_id)s, %(attempt)s, %(call_index)s, %(tool)s, %(args_json)s, %(ok)s,
     %(error_code)s, %(error_message)s, %(duration_ms)s, %(output_bytes)s)
ON CONFLICT (job_id, attempt, call_index) DO NOTHING
RETURNING id;
