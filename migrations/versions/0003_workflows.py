"""workflows and workflow_steps: durable multi-step workflows on the queue

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-15

"""
from __future__ import annotations

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None

_UPGRADE_SQL = """
CREATE TABLE workflows (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    type TEXT NOT NULL,
    input JSONB NOT NULL DEFAULT '{}'::jsonb,
    state TEXT NOT NULL DEFAULT 'running'
        CHECK (state IN ('running', 'succeeded', 'failed')),
    dedupe_key TEXT,
    failed_step TEXT,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Same dedupe rule as jobs: partial unique index, duplicate start returns
-- the existing workflow (first input wins).
CREATE UNIQUE INDEX workflows_dedupe_key_uq
    ON workflows (dedupe_key)
    WHERE dedupe_key IS NOT NULL;

-- The per-instance snapshot of the step list: names, positions and delays
-- are frozen at start_workflow; step functions are resolved by name at run
-- time. No 'running' step state: the carrying JOB is the in-flight signal.
CREATE TABLE workflow_steps (
    workflow_id UUID NOT NULL REFERENCES workflows (id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position >= 0),
    name TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'succeeded', 'failed')),
    output JSONB,
    delay_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
    PRIMARY KEY (workflow_id, position)
);

-- The reconciler reads each running workflow's first non-succeeded step.
CREATE INDEX workflow_steps_open_idx
    ON workflow_steps (workflow_id)
    WHERE state <> 'succeeded';
"""


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS workflow_steps;")
    op.execute("DROP TABLE IF EXISTS workflows;")
