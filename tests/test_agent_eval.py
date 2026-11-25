"""The evaluation runner: no model tokens, only the scoring path and the plan."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]


def _runner() -> Any:
    path = REPO / "scripts" / "agent_eval.py"
    spec = importlib.util.spec_from_file_location("agent_eval", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_scoring_ladder_is_green_for_every_task() -> None:
    runner = _runner()
    tasks = runner.load_tasks()

    assert len(tasks) == 12
    assert runner.validate(tasks) == 0


def test_every_task_has_a_target_config() -> None:
    runner = _runner()

    for task in runner.load_tasks():
        assert task.target in runner.TARGETS, task.id
        target = runner.TARGETS[task.target]
        assert target.suite, target.name
        assert target.visible_cases.name != target.hidden_cases.name


def test_a_provider_error_is_an_error_not_a_success() -> None:
    runner = _runner()
    task = runner.load_tasks()[0]

    record = runner.score(
        task,
        patch_proposed=False,
        refusal_codes=(),
        gate_category="",
        suite_status="unavailable",
        result=None,
        forced_terminal=False,
        error="ModelCallFailed: server: HTTP 502",
    )

    assert record["outcome"] == "error"
    assert record["successful"] is False
    assert "HTTP 502" in record["detail"]


def test_the_plan_interleaves_the_two_rounds_and_is_seeded() -> None:
    runner = _runner()
    tasks = runner.load_tasks()

    first = runner.plan(tasks, seed=7, repeats=2)
    again = runner.plan(tasks, seed=7, repeats=2)
    other = runner.plan(tasks, seed=8, repeats=2)

    assert [key for key, _ in first] == [key for key, _ in again]
    assert first != other, "a different seed must be a different order"
    assert [key for key, _ in first][:2] == [f"{first[0][1].id}#1", f"{first[0][1].id}#2"]
    assert len(first) == 24