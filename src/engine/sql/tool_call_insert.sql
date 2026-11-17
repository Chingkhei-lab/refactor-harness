-- One row per executed tool call. The UNIQUE (job_id, attempt, call_index)
-- is the dedupe: a retried step re-executes its calls from call_index 0 and
-- ON CONFLICT DO NOTHING keeps the first record — no duplicate rows.
--
-- Arguments are NEVER stored verbatim (apply_patch arguments are megabytes
-- of repository content and can contain repo secrets): args_sha256 is the
-- digest of the canonical JSON (validated args, or raw args on refusal),
-- args_bytes its size, args_preview a 200-char prefix for triage.
INSERT INTO tool_calls
    (job_id, attempt, call_index, tool, args_sha256, args_bytes, args_preview,
     ok, error_code, error_message, duration_ms, output_bytes)
VALUES
    (%(job_id)s, %(attempt)s, %(call_index)s, %(tool)s, %(args_sha256)s,
     %(args_bytes)s, %(args_preview)s, %(ok)s, %(error_code)s,
     %(error_message)s, %(duration_ms)s, %(output_bytes)s)
ON CONFLICT (job_id, attempt, call_index) DO NOTHING
RETURNING id;
