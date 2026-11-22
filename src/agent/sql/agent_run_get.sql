SELECT id, task_id, provider, model, system_prompt_version, system_prompt_sha256,
       temperature, budget, status, outcome, reason, created_at
  FROM agent_runs
 WHERE id = %(id)s
