-- Redelivery is keyed on STEP IDENTITY, not on content: run_ref carries the
-- workflow id and step position (see patchlog.append_patch). State hashes
-- are content hashes, so a workspace can legitimately return to an earlier
-- state (A->B, revert B->A, then a new A->B patch from a DIFFERENT step) —
-- a (parent hash, patch bytes) lookup cannot tell that apart from a
-- redelivery; a step identity can.
SELECT workspace_id, seq, parent_state_hash, new_state_hash, patch_blob_hash,
       run_ref, created_at
FROM workspace_patches
WHERE workspace_id = %(workspace_id)s
  AND run_ref = %(run_ref)s;
