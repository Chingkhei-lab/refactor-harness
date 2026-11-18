"""sandbox_runs: one measurement record per sandbox execution

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-17

Checkpoint 9, task 2: the sandbox-test-run workflow stores one row per
sandbox execution. The identity is the RUN step's (job_id, attempt) — the
execution that actually happened — written by the report step inside its
advance transaction, so the row commits atomically with the workflow's
progress. A retried report step (or a redelivered one) upserts the SAME
key: the constraint plus ON CONFLICT DO UPDATE keeps exactly one row per
execution. A NEW run attempt (worker died mid-run) is a NEW execution with
its own row — sandbox results are measurements, never effects, so we do
not promise identical outputs across attempts; the workflow's recorded
result is the committing attempt's.
"""
from __future__ import annotations

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | None = None
depends_on: str | None = None

_UPGRADE_SQL = """
CREATE TABLE sandbox_runs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- the RUN step's identity: the sandbox execution this row measures
    job_id UUID NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    attempt INTEGER NOT NULL
        CHECK (attempt >= 1),
    workflow_id UUID REFERENCES workflows (id) ON DELETE SET NULL,
    workspace_id BIGINT REFERENCES workspaces (id) ON DELETE SET NULL,
    -- install phase: cache key = sha256(requirements bytes + base image
    -- digest); the image id actually used is recorded per run (builds are
    -- functionally equivalent, not byte-identical, so the ID is the truth)
    install_key TEXT NOT NULL DEFAULT ''
        CHECK (install_key = '' OR install_key ~ '^[0-9a-f]{64}$'),
    install_image TEXT NOT NULL DEFAULT '',
    install_cached BOOLEAN NOT NULL DEFAULT FALSE,
    install_duration_ms BIGINT
        CHECK (install_duration_ms IS NULL OR install_duration_ms >= 0),
    -- run phase: measurements only (exit/flags/hashes/summary; the full
    -- output is NOT stored — its digest and a short summary are)
    image TEXT NOT NULL DEFAULT '',
    exit_code INTEGER,
    timed_out BOOLEAN NOT NULL DEFAULT FALSE,
    oom_killed BOOLEAN NOT NULL DEFAULT FALSE,
    kill_failed BOOLEAN NOT NULL DEFAULT FALSE,
    stdout_sha256 TEXT
        CHECK (stdout_sha256 IS NULL OR stdout_sha256 ~ '^[0-9a-f]{64}$'),
    stderr_sha256 TEXT
        CHECK (stderr_sha256 IS NULL OR stderr_sha256 ~ '^[0-9a-f]{64}$'),
    output_truncated BOOLEAN NOT NULL DEFAULT FALSE,
    output_summary TEXT,
    duration_ms BIGINT
        CHECK (duration_ms IS NULL OR duration_ms >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (job_id, attempt)
);
"""

_DOWNGRADE_SQL = """
DROP TABLE sandbox_runs;
"""


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)
