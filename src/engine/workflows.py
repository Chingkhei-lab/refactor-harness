"""Durable workflows built on the job engine: one job per step (Phase 2).

Design: docs/design-workflows.md; decision: docs/adr/002-workflows-on-jobs.md.
The invariant is the advance transaction — the step's output and the carrying
job's completion commit together, with `complete` last under the claim token,
in lock order job -> step -> workflow.
"""

from __future__ import annotations

import importlib
import importlib.util
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Json

from . import db, jobs
from .config import Settings, load_settings
from .db import Row
from .worker import Handler, HandlerContext

_SQL_DIR = Path(__file__).parent / "sql"


def _load_sql(name: str) -> str:
    return (_SQL_DIR / name).read_text(encoding="utf-8")


_CREATE_WORKFLOW_SQL = _load_sql("create_workflow.sql")
_CREATE_STEPS_SQL = _load_sql("create_workflow_steps.sql")
_COMPLETE_STEP_SQL = _load_sql("complete_step.sql")
_SUCCEED_WORKFLOW_SQL = _load_sql("succeed_workflow.sql")
_LOAD_WORKFLOW_SQL = _load_sql("load_workflow.sql")
_LOAD_STEPS_SQL = _load_sql("load_workflow_steps.sql")

WORKFLOW_JOB_TYPE = "workflow_step"

StepFn = Callable[["StepContext", dict[str, Any]], dict[str, Any] | None]
AfterCompleteCallback = Callable[[psycopg.Connection[Row]], None]


def step_job_dedupe_key(workflow_id: UUID, position: int) -> str:
    """Exactly one job per step, forever: duplicate enqueue returns it."""
    return f"wf:{workflow_id}:step:{position}"


def effect_key(workflow_id: UUID, step_name: str) -> str:
    """Idempotency key for a step's side effect: one row per step, ever."""
    return f"{workflow_id}:{step_name}"


@dataclass(frozen=True)
class StepDef:
    """A step as authored: function, its own retry budget, optional delay."""

    name: str
    fn: StepFn
    max_attempts: int = 5
    delay_seconds: float = 0.0


@dataclass(frozen=True)
class WorkflowDef:
    type: str
    steps: tuple[StepDef, ...]

    def __post_init__(self) -> None:
        if not self.steps:
            raise ValueError(f"workflow {self.type!r} needs at least one step")
        names = [step.name for step in self.steps]
        if len(set(names)) != len(names):
            raise ValueError(f"workflow {self.type!r} has duplicate step names")

    def step_by_name(self, name: str) -> StepDef:
        for step in self.steps:
            if step.name == name:
                return step
        raise KeyError(f"no step function named {name!r} in workflow {self.type!r}")


def load_workflows(spec: str) -> dict[str, WorkflowDef]:
    """Load a WORKFLOWS dict from a dotted module or a .py file path."""
    if spec.endswith(".py"):
        module_spec = importlib.util.spec_from_file_location("engine_workflows", spec)
        if module_spec is None or module_spec.loader is None:
            raise ImportError(f"cannot load workflows from {spec!r}")
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
    else:
        module = importlib.import_module(spec)
    workflows = getattr(module, "WORKFLOWS", None)
    if not isinstance(workflows, dict):
        raise TypeError(f"{spec!r} must define a WORKFLOWS dict")
    return {str(key): value for key, value in workflows.items()}


@dataclass(frozen=True)
class StepContext:
    """What a step function sees; the handler-level ctx stays out of sight."""

    workflow_id: UUID
    workflow_type: str
    step: str
    attempt: int
    effect_key: str
    lease_lost: threading.Event
    _job_ctx: HandlerContext = field(repr=False)

    def after_complete(self, callback: AfterCompleteCallback) -> None:
        """Register work for the advance transaction (with the job complete).

        The callback receives the worker's connection, must use only it, and
        must not commit or open its own transaction.
        """
        self._job_ctx.after_complete(callback)


@dataclass(frozen=True)
class Workflow:
    id: UUID
    type: str
    input: dict[str, Any]
    state: str
    dedupe_key: str | None
    failed_step: str | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class WorkflowStep:
    workflow_id: UUID
    position: int
    name: str
    state: str
    output: dict[str, Any] | None
    delay_seconds: float


def _to_workflow(row: Row) -> Workflow:
    return Workflow(
        id=row["id"],
        type=row["type"],
        input=row["input"],
        state=row["state"],
        dedupe_key=row["dedupe_key"],
        failed_step=row["failed_step"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _to_step(row: Row) -> WorkflowStep:
    return WorkflowStep(
        workflow_id=row["workflow_id"],
        position=row["position"],
        name=row["name"],
        state=row["state"],
        output=row["output"],
        delay_seconds=row["delay_seconds"],
    )


def start_workflow(
    conn: psycopg.Connection[Row],
    *,
    wf_type: str,
    definition: WorkflowDef,
    input: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
    settings: Settings | None = None,
) -> Workflow:
    """Create the workflow, its step snapshot, and step 0's job — atomically.

    A duplicate dedupe_key returns the existing workflow without creating
    extra rows (the first input wins, like job payloads).
    """
    settings = settings or load_settings()
    with conn.transaction():
        row = conn.execute(
            _CREATE_WORKFLOW_SQL,
            {
                "wf_type": wf_type,
                "input": Json(input if input is not None else {}),
                "dedupe_key": dedupe_key,
            },
        ).fetchone()
        if row is None:
            raise RuntimeError("create_workflow did not return a row")
        workflow = _to_workflow(row)
        if not row["inserted"]:
            return workflow  # duplicate: the existing workflow, untouched

        conn.execute(
            _CREATE_STEPS_SQL,
            {
                "workflow_id": workflow.id,
                "positions": list(range(len(definition.steps))),
                "names": [step.name for step in definition.steps],
                "delays": [step.delay_seconds for step in definition.steps],
            },
        )
        first = definition.steps[0]
        jobs.enqueue(
            conn,
            job_type=WORKFLOW_JOB_TYPE,
            payload={"workflow_id": str(workflow.id), "position": 0},
            dedupe_key=step_job_dedupe_key(workflow.id, 0),
            delay_seconds=first.delay_seconds,
            max_attempts=first.max_attempts,
            settings=settings,
        )
    return workflow


def load_workflow_state(
    conn: psycopg.Connection[Row], workflow_id: UUID
) -> tuple[Workflow, list[WorkflowStep]]:
    """Read the workflow row and its step snapshot, ordered by position."""
    row = conn.execute(_LOAD_WORKFLOW_SQL, {"workflow_id": workflow_id}).fetchone()
    if row is None:
        raise LookupError(f"workflow {workflow_id} not found")
    steps = [
        _to_step(step_row)
        for step_row in conn.execute(
            _LOAD_STEPS_SQL, {"workflow_id": workflow_id}
        ).fetchall()
    ]
    return _to_workflow(row), steps


def _advance_after_complete(
    conn: psycopg.Connection[Row],
    *,
    workflow: Workflow,
    definition: WorkflowDef,
    steps: list[WorkflowStep],
    position: int,
    output: dict[str, Any],
) -> None:
    """Step bookkeeping inside the advance transaction (after the complete).

    Lock order: the job row is already locked by the fenced complete; the step
    row is next, the workflow row last, and the next job's INSERT is final.
    Any exception here rolls back the complete as well.
    """
    conn.execute(
        _COMPLETE_STEP_SQL,
        {
            "workflow_id": workflow.id,
            "position": position,
            "output": Json(output),
        },
    )
    next_position = position + 1
    if next_position < len(steps):
        next_row = steps[next_position]
        next_def = definition.step_by_name(next_row.name)  # KeyError = loud fail
        jobs.enqueue(
            conn,
            job_type=WORKFLOW_JOB_TYPE,
            payload={"workflow_id": str(workflow.id), "position": next_position},
            dedupe_key=step_job_dedupe_key(workflow.id, next_position),
            delay_seconds=next_row.delay_seconds,  # the snapshot's delay
            max_attempts=next_def.max_attempts,
        )
    else:
        conn.execute(_SUCCEED_WORKFLOW_SQL, {"workflow_id": workflow.id})


def _ensure_advanced_after_complete(
    conn: psycopg.Connection[Row],
    *,
    workflow: Workflow,
    definition: WorkflowDef,
    steps: list[WorkflowStep],
    position: int,
) -> None:
    """Resume path: the step is already succeeded — only make state consistent.

    Completes the redelivered job (the worker's fenced complete) and ensures
    the next step's job exists (dedupe makes this a no-op when it does), or
    marks the workflow succeeded after the last step.
    """
    next_position = position + 1
    if next_position < len(steps):
        next_row = steps[next_position]
        next_def = definition.step_by_name(next_row.name)
        jobs.enqueue(
            conn,
            job_type=WORKFLOW_JOB_TYPE,
            payload={"workflow_id": str(workflow.id), "position": next_position},
            dedupe_key=step_job_dedupe_key(workflow.id, next_position),
            delay_seconds=next_row.delay_seconds,
            max_attempts=next_def.max_attempts,
        )
    else:
        conn.execute(_SUCCEED_WORKFLOW_SQL, {"workflow_id": workflow.id})


def make_workflow_handlers(
    workflows: dict[str, WorkflowDef],
    *,
    settings: Settings | None = None,
) -> dict[str, Handler]:
    """Handler registry entry: one generic handler dispatches every workflow.

    `settings` pins the database the handler reads from; None means the
    environment default (load_settings), which is what scripts rely on.
    """

    def workflow_step_handler(
        payload: dict[str, Any], ctx: HandlerContext
    ) -> dict[str, Any] | None:
        return _run_workflow_step(payload, ctx, workflows, settings)

    return {WORKFLOW_JOB_TYPE: workflow_step_handler}


def _run_workflow_step(
    payload: dict[str, Any],
    ctx: HandlerContext,
    workflows: dict[str, WorkflowDef],
    settings: Settings | None,
) -> dict[str, Any] | None:
    workflow_id = UUID(str(payload["workflow_id"]))
    position = int(payload["position"])

    conn = db.get_thread_connection(settings)
    workflow, steps = load_workflow_state(conn, workflow_id)
    definition = workflows.get(workflow.type)
    if definition is None:
        raise RuntimeError(f"no workflow definition for type {workflow.type!r}")
    if position >= len(definition.steps) or position >= len(steps):
        raise RuntimeError(
            f"step position {position} out of range for workflow {workflow.id}"
        )
    step_row = steps[position]
    step_def = definition.step_by_name(step_row.name)

    step_ctx = StepContext(
        workflow_id=workflow.id,
        workflow_type=workflow.type,
        step=step_row.name,
        attempt=ctx.attempt,
        effect_key=effect_key(workflow.id, step_row.name),
        lease_lost=ctx.lease_lost,
        _job_ctx=ctx,
    )

    if step_row.state == "succeeded":
        # Resume: skip the function, but still advance atomically with the
        # complete — a redelivered job must not linger half-handled.
        ctx.after_complete(
            lambda conn: _ensure_advanced_after_complete(
                conn,
                workflow=workflow,
                definition=definition,
                steps=steps,
                position=position,
            )
        )
        return {
            "workflow_id": str(workflow.id),
            "step": step_row.name,
            "position": position,
            "resumed": True,
        }

    prior: dict[str, Any] = {
        step.name: step.output
        for step in steps[:position]
        if step.state == "succeeded" and step.output is not None
    }
    output = step_def.fn(step_ctx, prior)
    output_dict: dict[str, Any] = output if output is not None else {}
    ctx.after_complete(
        lambda conn: _advance_after_complete(
            conn,
            workflow=workflow,
            definition=definition,
            steps=steps,
            position=position,
            output=output_dict,
        )
    )
    return {
        "workflow_id": str(workflow.id),
        "step": step_row.name,
        "position": position,
    }
