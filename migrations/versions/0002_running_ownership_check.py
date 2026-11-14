"""running jobs must have an owner and a lease

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-14

"""
from __future__ import annotations

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None

_CONSTRAINT_SQL = """
ALTER TABLE jobs
    ADD CONSTRAINT jobs_running_has_owner_check
    CHECK (
        state <> 'running'
        OR (locked_by IS NOT NULL AND lease_expires_at IS NOT NULL)
    );
"""


def upgrade() -> None:
    op.execute(_CONSTRAINT_SQL)


def downgrade() -> None:
    op.execute("ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_running_has_owner_check;")
