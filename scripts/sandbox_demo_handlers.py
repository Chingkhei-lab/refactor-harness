"""Workflow definitions for the sandbox demos and mini-chaos (checkpoint 9).

Loaded by the worker subprocesses via BOTH module env vars (the same wiring
scripts/chaos_workflow_handlers.py uses):

- ENGINE_HANDLERS_MODULE  -> HANDLERS, deliberately EMPTY: the generic
  'workflow_step' handler must come from ENGINE_WORKFLOWS_MODULE, and an
  empty dict keeps the engine.worker guard happy;
- ENGINE_WORKFLOWS_MODULE -> WORKFLOWS, the sandbox-test-run definition
  closed over a REAL Docker adapter (each worker process gets its own
  daemon connection; the worker is single-threaded, so one sandbox at a
  time per process — the slot budget is the multi-worker cap).

The exact allowlist pins the demo command; inputs carrying anything else
are refused at the prepare step as a permanent error.

Step budget: ENGINE_SANDBOX_STEP_BUDGET widens every StepDef.max_attempts
before the definition is snapshotted. Mini-chaos run 2 measured why: with
the authored budget of 5, 428 mid-job kills exhausted 42 workflows' retry
budgets and dead-lettered them — a murdered job costs an attempt like any
other failure, so a kill bombardment needs a budget sized to outlive it.
Unset leaves the authored default (5); the mini-chaos script sets 40.
"""

from __future__ import annotations

import dataclasses
import os
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (str(_REPO_ROOT / "src"),):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import docker  # noqa: E402

from engine.config import Settings, load_settings  # noqa: E402
from engine.sandbox import DockerSandboxAdapter  # noqa: E402
from engine.sandbox_workflow import (  # noqa: E402
    WORKFLOW_TYPE,
    SandboxRunnerClient,
    make_sandbox_test_run_workflow,
)
from engine.workflows import WorkflowDef  # noqa: E402

_DEMO_COMMAND = ("python", "src/main.py")
STEP_BUDGET_ENV = "ENGINE_SANDBOX_STEP_BUDGET"


def step_budget_from_env() -> int | None:
    """The env step budget, or None when unset (the authored default holds)."""
    raw = os.environ.get(STEP_BUDGET_ENV, "").strip()
    if not raw:
        return None
    budget = int(raw)
    if budget < 1:
        raise ValueError(f"{STEP_BUDGET_ENV} must be >= 1, got {raw!r}")
    return budget


def build_definition(
    client: SandboxRunnerClient,
    *,
    settings: Settings,
    allowed_commands: tuple[tuple[str, ...], ...] | None,
) -> WorkflowDef:
    """The sandbox-test-run definition with the env step budget applied.

    Shared by this module's WORKFLOWS (worker subprocesses) and by the
    mini-chaos script's start_workflow call, so every snapshot of the
    definition — worker-side or caller-side — carries the same budget.
    """
    definition = make_sandbox_test_run_workflow(
        client,
        settings=settings,
        allowed_commands=allowed_commands,
    )
    budget = step_budget_from_env()
    if budget is None:
        return definition
    return dataclasses.replace(
        definition,
        steps=tuple(
            dataclasses.replace(step, max_attempts=budget) for step in definition.steps
        ),
    )


WORKFLOWS: dict[str, Any] = {
    WORKFLOW_TYPE: build_definition(
        DockerSandboxAdapter(docker.from_env()),
        settings=load_settings(),
        allowed_commands=(_DEMO_COMMAND,),
    ),
}
HANDLERS: dict[str, Any] = {}
