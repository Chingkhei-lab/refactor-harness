-- One measurement row per sandbox execution, keyed by the RUN step's
-- (job_id, attempt). The report step writes this inside its advance
-- transaction, so the row commits atomically with the workflow progress.
-- ON CONFLICT DO UPDATE makes a retried/redelivered report step idempotent:
-- the same execution rewrites its own row; a NEW run attempt is a new
-- (job_id, attempt) and gets its own row — sandbox results are
-- measurements, never effects.
INSERT INTO sandbox_runs
    (job_id, attempt, workflow_id, workspace_id,
     install_key, install_image, install_cached, install_duration_ms,
     image, exit_code, timed_out, oom_killed, kill_failed,
     stdout_sha256, stderr_sha256, output_truncated, output_summary,
     duration_ms)
VALUES
    (%(job_id)s, %(attempt)s, %(workflow_id)s, %(workspace_id)s,
     %(install_key)s, %(install_image)s, %(install_cached)s,
     %(install_duration_ms)s,
     %(image)s, %(exit_code)s, %(timed_out)s, %(oom_killed)s,
     %(kill_failed)s,
     %(stdout_sha256)s, %(stderr_sha256)s, %(output_truncated)s,
     %(output_summary)s, %(duration_ms)s)
ON CONFLICT (job_id, attempt) DO UPDATE SET
    workflow_id = EXCLUDED.workflow_id,
    workspace_id = EXCLUDED.workspace_id,
    install_key = EXCLUDED.install_key,
    install_image = EXCLUDED.install_image,
    install_cached = EXCLUDED.install_cached,
    install_duration_ms = EXCLUDED.install_duration_ms,
    image = EXCLUDED.image,
    exit_code = EXCLUDED.exit_code,
    timed_out = EXCLUDED.timed_out,
    oom_killed = EXCLUDED.oom_killed,
    kill_failed = EXCLUDED.kill_failed,
    stdout_sha256 = EXCLUDED.stdout_sha256,
    stderr_sha256 = EXCLUDED.stderr_sha256,
    output_truncated = EXCLUDED.output_truncated,
    output_summary = EXCLUDED.output_summary,
    duration_ms = EXCLUDED.duration_ms,
    updated_at = now()
RETURNING id;
