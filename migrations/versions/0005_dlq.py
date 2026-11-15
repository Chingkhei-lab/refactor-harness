"""jobs.replay_count and the dead_letters view

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-16

"""
from __future__ import annotations

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None

_UPGRADE_SQL = """
-- Replay extends the retry budget instead of rewinding attempts (ADR 002);
-- replay_count is the era counter: how many times this row was replayed.
ALTER TABLE jobs ADD COLUMN replay_count INTEGER NOT NULL DEFAULT 0;

-- The reconciler's rule 1 and the DLQ both scan dead rows; keep that scan
-- partial and tiny like the claim/lease indexes.
CREATE INDEX jobs_dead_idx ON jobs (updated_at) WHERE state = 'dead';

-- One place to see every dead job with its workflow/step context. Plain
-- (non-workflow) dead jobs appear too, with the workflow columns NULL.
-- The CASE guards the payload casts: CASE is documented to evaluate its
-- branches lazily, so arbitrary non-workflow payloads are never cast.
CREATE VIEW dead_letters AS
SELECT
    j.id,
    j.type,
    j.payload,
    j.attempts,
    j.max_attempts,
    j.replay_count,
    j.last_error,
    j.created_at,
    j.updated_at,
    CASE WHEN j.type = 'workflow_step'
        THEN (j.payload ->> 'workflow_id')::uuid
    END AS workflow_id,
    CASE WHEN j.type = 'workflow_step'
        THEN (j.payload ->> 'position')::int
    END AS step_position,
    s.name AS step_name,
    wf.type AS workflow_type,
    wf.state AS workflow_state
FROM jobs j
LEFT JOIN workflow_steps s
    ON s.workflow_id = CASE WHEN j.type = 'workflow_step'
        THEN (j.payload ->> 'workflow_id')::uuid END
   AND s.position = CASE WHEN j.type = 'workflow_step'
        THEN (j.payload ->> 'position')::int END
LEFT JOIN workflows wf ON wf.id = s.workflow_id
WHERE j.state = 'dead';
"""

_DOWNGRADE_SQL = """
DROP VIEW IF EXISTS dead_letters;
DROP INDEX IF EXISTS jobs_dead_idx;
ALTER TABLE jobs DROP COLUMN IF EXISTS replay_count;
"""


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)
