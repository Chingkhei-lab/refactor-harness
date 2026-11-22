INSERT INTO agent_runs (
    id, task_id, provider, model, system_prompt_version, system_prompt_sha256,
    temperature, budget, status, outcome, reason
) VALUES (
    %(id)s, %(task_id)s, %(provider)s, %(model)s, %(prompt_version)s, %(prompt_sha256)s,
    %(temperature)s, CAST(%(budget)s AS jsonb), %(status)s, %(outcome)s, %(reason)s
)
ON CONFLICT (id) DO UPDATE
    SET status = EXCLUDED.status,
        outcome = EXCLUDED.outcome,
        reason = EXCLUDED.reason,
        updated_at = now()
