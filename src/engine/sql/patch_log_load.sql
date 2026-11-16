-- Load the ordered patch log for a rebuild: base snapshot + patches in seq
-- order reproduce the tree on any worker.
SELECT workspace_id, seq, parent_state_hash, new_state_hash, patch_blob_hash,
       run_ref, created_at
FROM workspace_patches
WHERE workspace_id = %(workspace_id)s
ORDER BY seq;
