-- What sits at a seq: the idempotency pre-check ("is the right seq already
-- taken, and by whom?") before the CAS is attempted.
SELECT workspace_id, seq, parent_state_hash, new_state_hash, patch_blob_hash,
       run_ref, created_at
FROM workspace_patches
WHERE workspace_id = %(workspace_id)s
  AND seq = %(seq)s;
