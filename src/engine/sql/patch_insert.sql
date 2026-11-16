-- The patch row for the seq the CAS just handed out. Must run in the SAME
-- transaction as patch_cas.sql: the (workspace_id, seq) key exists only
-- while the head advanced, and both commit or roll back together.
INSERT INTO workspace_patches
    (workspace_id, seq, parent_state_hash, new_state_hash, patch_blob_hash, run_ref)
VALUES
    (%(workspace_id)s, %(seq)s, %(parent_state_hash)s, %(new_state_hash)s,
     %(patch_blob_hash)s, %(run_ref)s)
RETURNING workspace_id, seq, parent_state_hash, new_state_hash, patch_blob_hash,
          run_ref, created_at;
