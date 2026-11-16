-- Create a workspace: the base snapshot is a blob (tar of regular files),
-- and the patch-log head starts at the base tree's state hash with seq 0.
INSERT INTO workspaces (base_snapshot_hash, head_seq, head_hash)
VALUES (%(base_snapshot_hash)s, 0, %(head_hash)s)
RETURNING id, base_snapshot_hash, head_seq, head_hash, created_at;
