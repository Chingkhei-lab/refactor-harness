-- The patch-log append is a compare-and-swap on the head, not a separate
-- lock: one UPDATE takes the workspaces row lock, advances the head, and
-- RETURNING hands back the exclusively-owned seq. Zero rows means the
-- parent hash no longer matches the head — a stale or conflicting append —
-- and the caller rolls its whole transaction back. (ADR 002: this row
-- lock is taken after the job, step and workflow locks, last in the order.)
UPDATE workspaces
SET head_seq = head_seq + 1,
    head_hash = %(new_state_hash)s
WHERE id = %(workspace_id)s
  AND head_hash = %(parent_state_hash)s
RETURNING head_seq, head_hash;
