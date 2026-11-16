"""content-addressed blobs, workspaces, the patch log, and tool_calls

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-16

"""
from __future__ import annotations

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None

_UPGRADE_SQL = """
-- Content-addressed blob store: base snapshots (tars of regular files) and
-- patch documents (canonical JSON). The hash IS the identity, so a duplicate
-- upload is ON CONFLICT DO NOTHING and the loader re-verifies sha256(data)
-- == hash on every read: corruption is detected, never silently served.
CREATE TABLE blobs (
    hash TEXT PRIMARY KEY
        CHECK (hash ~ '^[0-9a-f]{64}$'),
    data BYTEA NOT NULL,
    size BIGINT NOT NULL
        CHECK (size >= 0)
);

-- A durable workspace: the base snapshot plus an ordered patch log, held in
-- the two tables below. head_seq/head_hash are the append's CAS token — the
-- append advances them with a conditional UPDATE on head_hash equal to the
-- parent state hash, so a stale worker's append matches zero rows and its
-- whole transaction rolls back.
CREATE TABLE workspaces (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    base_snapshot_hash TEXT NOT NULL REFERENCES blobs (hash),
    head_seq BIGINT NOT NULL DEFAULT 0
        CHECK (head_seq >= 0),
    head_hash TEXT NOT NULL
        CHECK (head_hash ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The log itself: seq is exclusively owned by the CAS (only a winning
-- append inserts at the seq the CAS handed back), so the PK enforces the
-- chain's order. parent_state_hash -> new_state_hash is the hash chain a
-- rebuild verifies. The (workspace_id, parent_state_hash) index is the
-- idempotent-redelivery lookup: same parent + same patch bytes = same
-- patch, already applied.
CREATE TABLE workspace_patches (
    workspace_id BIGINT NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
    seq BIGINT NOT NULL
        CHECK (seq >= 1),
    parent_state_hash TEXT NOT NULL
        CHECK (parent_state_hash ~ '^[0-9a-f]{64}$'),
    new_state_hash TEXT NOT NULL
        CHECK (new_state_hash ~ '^[0-9a-f]{64}$'),
    patch_blob_hash TEXT NOT NULL REFERENCES blobs (hash),
    run_ref TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, seq)
);

CREATE INDEX workspace_patches_parent_idx
    ON workspace_patches (workspace_id, parent_state_hash);

-- One row per executed tool call. The UNIQUE (job_id, attempt, call_index)
-- is the dedupe: a retried step re-executes from call_index 0 and
-- ON CONFLICT DO NOTHING keeps the first record. args_json holds the
-- VALIDATED input (or the raw args on refusal), NUL-sanitized before jsonb
-- (jsonb rejects \\u0000), capped at 32 KiB by the writer.
CREATE TABLE tool_calls (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    attempt INTEGER NOT NULL
        CHECK (attempt >= 1),
    call_index INTEGER NOT NULL
        CHECK (call_index >= 0),
    tool TEXT NOT NULL,
    args_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    ok BOOLEAN NOT NULL,
    error_code TEXT,
    error_message TEXT,
    duration_ms DOUBLE PRECISION NOT NULL
        CHECK (duration_ms >= 0),
    output_bytes INTEGER NOT NULL
        CHECK (output_bytes >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (job_id, attempt, call_index)
);
"""

_DOWNGRADE_SQL = """
DROP TABLE tool_calls;
DROP TABLE workspace_patches;
DROP TABLE workspaces;
DROP TABLE blobs;
"""


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)
