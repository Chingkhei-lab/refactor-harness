"""agent_runs and llm_calls: one committed row per model turn

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-22

Checkpoint 13, task 1 (ADR 006 D2-D4). A model call is nondeterministic and
costs money, so it is a workflow step with a durable record rather than a side
effect: `llm_calls` stores one row per (run, turn) — the request hash, the full
response, provider, model, tokens, latency, attempts and status.

`UNIQUE (run_id, turn)` is the idempotency key, and the insert uses
ON CONFLICT DO NOTHING, so a redelivered or resumed turn writes nothing instead
of overwriting a committed response with a fresh one. On resume, a committed turn
is replayed from this table and is ONLY replayed if the request rebuilt now
hashes to `request_sha256`; a mismatch aborts the run, because reusing a stored
answer for a different question would make the log fiction.

`response` is the assistant turn verbatim, including provider extras: Gemini 3.x
requires its `thought_signature` to be echoed back on the next turn or every
subsequent request is a 400, so a reconstruction from our own fields would not be
replayable.
"""
from __future__ import annotations

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | None = None
depends_on: str | None = None

_UPGRADE_SQL = """
CREATE TABLE agent_runs (
    id UUID PRIMARY KEY,
    task_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    -- the prompt is an experiment input, so its identity is recorded with the run
    system_prompt_version TEXT NOT NULL,
    system_prompt_sha256 TEXT NOT NULL
        CHECK (system_prompt_sha256 ~ '^[0-9a-f]{64}$'),
    temperature NUMERIC(3, 2) NOT NULL DEFAULT 0
        CHECK (temperature >= 0 AND temperature <= 2),
    budget JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'finished', 'gave_up', 'budget_exhausted', 'failed')),
    outcome TEXT
        CHECK (outcome IS NULL OR outcome IN
               ('finished', 'gave_up', 'budget_exhausted', 'failed')),
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE llm_calls (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES agent_runs (id) ON DELETE CASCADE,
    turn INTEGER NOT NULL CHECK (turn >= 0),
    request_sha256 TEXT NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    -- the assistant turn as committed, provider extras included; never rewritten
    response JSONB NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    tokens_prompt INTEGER CHECK (tokens_prompt IS NULL OR tokens_prompt >= 0),
    tokens_completion INTEGER CHECK (tokens_completion IS NULL OR tokens_completion >= 0),
    latency_ms BIGINT CHECK (latency_ms IS NULL OR latency_ms >= 0),
    attempts INTEGER NOT NULL DEFAULT 1 CHECK (attempts >= 1),
    status TEXT NOT NULL CHECK (status IN ('ok', 'error')),
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT llm_calls_run_turn_key UNIQUE (run_id, turn)
);
"""

_DOWNGRADE_SQL = """
DROP TABLE IF EXISTS llm_calls;
DROP TABLE IF EXISTS agent_runs;
"""


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)
