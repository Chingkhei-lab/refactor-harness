-- The patch-log head: the CAS token of appends. head_seq + head_hash are
-- read for the idempotency pre-checks and are the compare-and-swap
-- predicate; the row lock this UPDATE takes serializes concurrent appends.
SELECT id, base_snapshot_hash, head_seq, head_hash, created_at
FROM workspaces
WHERE id = %(workspace_id)s;
