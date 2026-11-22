INSERT INTO llm_calls (
    run_id, turn, request_sha256, response, provider, model,
    tokens_prompt, tokens_completion, latency_ms, attempts, status, error
) VALUES (
    %(run_id)s, %(turn)s, %(request_sha256)s, CAST(%(response)s AS jsonb), %(provider)s, %(model)s,
    %(tokens_prompt)s, %(tokens_completion)s, %(latency_ms)s, %(attempts)s, %(status)s, %(error)s
)
ON CONFLICT (run_id, turn) DO NOTHING
RETURNING turn
