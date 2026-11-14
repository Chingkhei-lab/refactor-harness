"""jobs table

Revision ID: 0001
Revises:
Create Date: 2026-11-14

"""
from __future__ import annotations

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None

_JOBS_TABLE_SQL = """
CREATE TABLE jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    state TEXT NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued', 'running', 'succeeded', 'dead')),
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    run_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_by TEXT,
    lease_expires_at TIMESTAMPTZ,
    last_heartbeat_at TIMESTAMPTZ,
    result JSONB,
    last_error TEXT,
    dedupe_key TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_INDEXES_SQL = [
    # Claim path: next eligible job is state = 'queued' AND run_at <= now(),
    # ordered by (run_at, id). Partial: only queued rows are ever looked up
    # here, so the index stays tiny as succeeded/dead rows accumulate.
    "CREATE INDEX jobs_claim_ready_idx ON jobs (run_at, id) WHERE state = 'queued';",
    # Reaper path: expired leases among running jobs. Partial: only running
    # rows have a meaningful lease_expires_at to scan.
    "CREATE INDEX jobs_lease_expires_idx ON jobs (lease_expires_at) WHERE state = 'running';",
    # Dedupe path: duplicate enqueue must return the existing job. Partial:
    # NULL keys are always distinct and must not collide.
    "CREATE UNIQUE INDEX jobs_dedupe_key_uq ON jobs (dedupe_key) WHERE dedupe_key IS NOT NULL;",
]


def upgrade() -> None:
    op.execute(_JOBS_TABLE_SQL)
    for statement in _INDEXES_SQL:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS jobs;")
