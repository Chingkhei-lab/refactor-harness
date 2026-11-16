"""Workflow definitions and step handlers for the chaos-v2 exit check.

Loaded by the worker via BOTH module env vars (see scripts/chaos_workflows.py):

- ENGINE_HANDLERS_MODULE  -> HANDLERS, deliberately EMPTY: the generic
  'workflow_step' handler must come from ENGINE_WORKFLOWS_MODULE, and an
  empty dict keeps the engine.worker guard happy;
- ENGINE_WORKFLOWS_MODULE -> WORKFLOWS, the three chaos-v2 definitions.

Three workflow types, 4 steps each, one 2-second delay step at position 2:

- chaos_v2      : normal; sums [1, 3, 6, 10] (addends 1..4).
- chaos_v2_fail : step "boom" (position 1, addend 2) raises RuntimeError on
                  every attempt (max_attempts=3) UNLESS a pardon row exists
                  for its job — that is the replay phase's lever.
- chaos_v2_nr   : step "poison" (position 1, addend 2) raises
                  NonRetryableError on the first attempt.

Every step's output is a PURE function of its inputs (running sum of the
previous step's saved output plus a constant addend), so a retry recomputes
the same output: the chain is checkable and identical across attempts. Each
step records its effect BEFORE any raise (a killed attempt can leave the
effect committed while the step output is not saved — the engine is still
correct because effects(effect_key UNIQUE) + ON CONFLICT DO NOTHING make the
retry's insert a no-op, and the recomputed output is identical).
"""

from __future__ import annotations

import os
import random
import socket
import sys
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (str(_REPO_ROOT / "src"),):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from psycopg.types.json import Json  # noqa: E402

from engine import db  # noqa: E402
from engine.config import load_settings  # noqa: E402
from engine.jobs import NonRetryableError  # noqa: E402
from engine.workflows import StepContext, StepDef, WorkflowDef  # noqa: E402

EXECUTIONS_SQL = """
INSERT INTO executions (job_id, attempt, effect_key, worker)
VALUES (%(job_id)s, %(attempt)s, %(effect_key)s, %(worker)s)
ON CONFLICT (job_id, attempt) DO NOTHING
RETURNING job_id
"""

EFFECT_SQL = """
INSERT INTO effects (effect_key, workflow_id, step_name, output, job_id, attempt, worker)
VALUES (%(effect_key)s, %(workflow_id)s, %(step_name)s, %(output)s,
        %(job_id)s, %(attempt)s, %(worker)s)
ON CONFLICT (effect_key) DO NOTHING
RETURNING effect_key
"""

VIOLATION_SQL = """
INSERT INTO violations (job_id, attempt, worker, detail)
VALUES (%(job_id)s, %(attempt)s, %(worker)s, %(detail)s)
"""

PARDON_SQL = "SELECT 1 AS pardoned FROM pardon WHERE job_id = %(job_id)s"

# Handler sleep per sum step: widens the window a hard kill can land in
# (the kill loop then measures how often a victim really held a running job).
SLEEP_RANGE_SECONDS = (0.020, 0.060)
DELAY_STEP_SECONDS = 2.0
SUM_BUDGET = 8  # normal steps' retry budget: kills must not dead them


def _connection() -> Any:
    return db.get_thread_connection(load_settings())


def _worker_label() -> str:
    return f"{socket.gethostname()}#{os.getpid()}"


def _record_execution_and_effect(
    ctx: StepContext, output: dict[str, Any]
) -> None:
    """Record the execution and the step's effect (idempotent by effect_key).

    The executions row is one per (job_id, attempt) — the claim-fenced
    identity of a single execution — so a PK conflict there is a real claim
    violation and goes into violations. The effects row is one per step
    FOREVER (effect_key UNIQUE + DO NOTHING): a retry's insert no-ops, which
    is exactly how "duplicate execution, no duplicate effect" holds.
    """
    job_id = ctx._job_ctx.job_id  # noqa: SLF001 — scaffolding reads the job ctx
    worker = _worker_label()
    conn = _connection()
    executed = conn.execute(
        EXECUTIONS_SQL,
        {
            "job_id": job_id,
            "attempt": ctx.attempt,
            "effect_key": ctx.effect_key,
            "worker": worker,
        },
    ).fetchone()
    if executed is None:
        conn.execute(
            VIOLATION_SQL,
            {
                "job_id": job_id,
                "attempt": ctx.attempt,
                "worker": worker,
                "detail": "duplicate execution for (job_id, attempt)",
            },
        )
    conn.execute(
        EFFECT_SQL,
        {
            "effect_key": ctx.effect_key,
            "workflow_id": ctx.workflow_id,
            "step_name": ctx.step,
            "output": Json(output),
            "job_id": job_id,
            "attempt": ctx.attempt,
            "worker": worker,
        },
    )


def _is_pardoned(ctx: StepContext) -> bool:
    """The replay phase's lever: a pardon row exempts this job from failing."""
    row = _connection().execute(
        PARDON_SQL, {"job_id": ctx._job_ctx.job_id}
    ).fetchone()
    return row is not None


def _prior_sum(prior: dict[str, Any], prev_name: str | None) -> int:
    """Pure read of the previous step's saved output (0 for step 0)."""
    if prev_name is None:
        return 0
    prev_output = prior.get(prev_name) or {}
    return int(prev_output.get("sum", 0))


def _sum_step(prev_name: str | None, addend: int) -> Any:
    def fn(ctx: StepContext, prior: dict[str, Any]) -> dict[str, Any]:
        total = _prior_sum(prior, prev_name) + addend
        output = {"sum": total, "addend": addend}
        _record_execution_and_effect(ctx, output)
        time.sleep(random.uniform(*SLEEP_RANGE_SECONDS))
        return output

    return fn


def _boom(ctx: StepContext, prior: dict[str, Any]) -> dict[str, Any]:
    """Always-failing step: effect first, then raise — unless pardoned."""
    total = _prior_sum(prior, "sum_a") + 2
    output = {"sum": total, "addend": 2}
    _record_execution_and_effect(ctx, output)
    if not _is_pardoned(ctx):
        raise RuntimeError("chaos_v2_fail: injected always-failing step")
    return output


def _poison(ctx: StepContext, prior: dict[str, Any]) -> dict[str, Any]:
    """NonRetryable step: effect first, then the error (job dies at once)."""
    total = _prior_sum(prior, "sum_a") + 2
    output = {"sum": total, "addend": 2}
    _record_execution_and_effect(ctx, output)
    raise NonRetryableError("chaos_v2_nr: injected non-retryable failure")


WORKFLOWS: dict[str, WorkflowDef] = {
    "chaos_v2": WorkflowDef(
        type="chaos_v2",
        steps=(
            StepDef("sum_a", _sum_step(None, 1), max_attempts=SUM_BUDGET),
            StepDef("sum_b", _sum_step("sum_a", 2), max_attempts=SUM_BUDGET),
            StepDef(
                "sum_c",
                _sum_step("sum_b", 3),
                max_attempts=SUM_BUDGET,
                delay_seconds=DELAY_STEP_SECONDS,
            ),
            StepDef("sum_d", _sum_step("sum_c", 4), max_attempts=SUM_BUDGET),
        ),
    ),
    "chaos_v2_fail": WorkflowDef(
        type="chaos_v2_fail",
        steps=(
            StepDef("sum_a", _sum_step(None, 1), max_attempts=SUM_BUDGET),
            StepDef("boom", _boom, max_attempts=3),
            StepDef(
                "sum_c",
                _sum_step("boom", 3),
                max_attempts=SUM_BUDGET,
                delay_seconds=DELAY_STEP_SECONDS,
            ),
            StepDef("sum_d", _sum_step("sum_c", 4), max_attempts=SUM_BUDGET),
        ),
    ),
    "chaos_v2_nr": WorkflowDef(
        type="chaos_v2_nr",
        steps=(
            StepDef("sum_a", _sum_step(None, 1), max_attempts=SUM_BUDGET),
            StepDef("poison", _poison, max_attempts=3),
            StepDef(
                "sum_c",
                _sum_step("poison", 3),
                max_attempts=SUM_BUDGET,
                delay_seconds=DELAY_STEP_SECONDS,
            ),
            StepDef("sum_d", _sum_step("sum_c", 4), max_attempts=SUM_BUDGET),
        ),
    ),
}

# Deliberately empty: 'workflow_step' is provided by WORKFLOWS above via
# make_workflow_handlers; a non-empty HANDLERS here would collide with it.
HANDLERS: dict[str, Any] = {}
