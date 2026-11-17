"""step-identity idempotency for the patch log; tool_calls args summary

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-16

Review fix, checkpoint 8 round 1: redelivery was keyed on
(parent_state_hash, patch_blob_hash), but state hashes are CONTENT hashes —
a workspace can return to an earlier state (A->B, revert B->A, then a new
A->B patch by a different step). The old lookup then reported the genuine
new append as redelivery and silently dropped it. Redelivery must be keyed
on STEP IDENTITY: run_ref carries the workflow id and step position, and a
partial UNIQUE index makes one recorded patch per (workspace, run_ref).

The same revision stops tool_calls from storing full arguments: apply_patch
arguments are megabytes of repository content (and can contain repo
secrets). What remains is the argument digest, its size, and a 200-char
preview.
"""
from __future__ import annotations

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | None = None
depends_on: str | None = None

_UPGRADE_SQL = """
-- One recorded patch per step identity (non-workspace callers leave
-- run_ref = '' and are exempt; the CAS still serializes them).
CREATE UNIQUE INDEX workspace_patches_run_ref_uq
    ON workspace_patches (workspace_id, run_ref)
    WHERE run_ref <> '';

-- Full arguments never stored: the digest identifies what was validated
-- (re-derivable from the args the step already logged elsewhere), the size
-- bounds it, and the preview keeps 200 chars for triage. The old JSONB
-- column could hold megabytes of repository content, including secrets.
ALTER TABLE tool_calls DROP COLUMN args_json;
ALTER TABLE tool_calls
    ADD COLUMN args_sha256 TEXT
        CHECK (args_sha256 ~ '^[0-9a-f]{64}$'),
    ADD COLUMN args_bytes BIGINT NOT NULL DEFAULT 0
        CHECK (args_bytes >= 0),
    ADD COLUMN args_preview TEXT
        CHECK (length(args_preview) <= 200);
"""

_DOWNGRADE_SQL = """
ALTER TABLE tool_calls
    DROP COLUMN args_preview,
    DROP COLUMN args_bytes,
    DROP COLUMN args_sha256;
ALTER TABLE tool_calls ADD COLUMN args_json JSONB NOT NULL DEFAULT '{}'::jsonb;
DROP INDEX workspace_patches_run_ref_uq;
"""


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)
