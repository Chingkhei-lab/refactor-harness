"""workflow_steps.max_attempts: snapshot the per-step retry budget

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-16

"""
from __future__ import annotations

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None

_UPGRADE_SQL = """
ALTER TABLE workflow_steps
    ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 5
        CHECK (max_attempts >= 1);
"""

_DOWNGRADE_SQL = """
ALTER TABLE workflow_steps DROP COLUMN IF EXISTS max_attempts;
"""


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)
