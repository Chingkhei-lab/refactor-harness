-- Idempotent redelivery: a patch identifies itself by (parent_state_hash,
-- content hash), so "the same patch is already recorded" is one lookup —
-- wherever it landed in the log. Same parent + same bytes = same patch.
SELECT workspace_id, seq, parent_state_hash, new_state_hash, patch_blob_hash,
       run_ref, created_at
FROM workspace_patches
WHERE workspace_id = %(workspace_id)s
  AND parent_state_hash = %(parent_state_hash)s
  AND patch_blob_hash = %(patch_blob_hash)s;
