SELECT turn, request_sha256, response, provider, model, status, error
  FROM llm_calls
 WHERE run_id = %(run_id)s
 ORDER BY turn
