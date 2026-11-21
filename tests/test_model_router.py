"""Unit tests for the model-router and bake-off mechanics that do not need a network.

The bake-off itself calls providers; these tests cover the parts that decide what a
run means: tool-call validation, argument normalisation, the workspace tool layer
and the Wilson interval used for the confidence bounds.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import bakeoff_tools  # noqa: E402
import model_bakeoff  # noqa: E402
import model_router  # noqa: E402


def test_a_well_formed_call_validates() -> None:
    assert bakeoff_tools.validate_arguments("read_file", {"path": "calc/core.py"}) == ""
    assert (
        bakeoff_tools.validate_arguments(
            "apply_patch",
            {"path": "calc/core.py", "old": "a", "new": "b", "precondition_sha": "0" * 64},
        )
        == ""
    )
    assert bakeoff_tools.validate_arguments("run_tests", {}) == ""


@pytest.mark.parametrize(
    ("name", "arguments", "fragment"),
    [
        ("read_file", None, "not a JSON object"),
        ("read_file", {}, "missing required argument"),
        ("read_file", {"path": 3}, "must be a string"),
        ("read_file", {"path": "calc/core.py", "start": "1"}, "must be an integer"),
        ("read_file", {"path": "calc/core.py", "extra": 1}, "unexpected argument"),
        ("delete_everything", {"path": "x"}, "unknown tool"),
        ("read_file", {"path": "../secrets"}, "escapes the workspace"),
        ("read_file", {"path": "/etc/passwd"}, "escapes the workspace"),
    ],
)
def test_a_malformed_call_is_named_and_refused(
    name: str, arguments: dict[str, object] | None, fragment: str
) -> None:
    reason = bakeoff_tools.validate_arguments(name, arguments)
    assert fragment in reason, reason


def test_arguments_that_are_not_json_are_not_repaired() -> None:
    """A provider reply with broken arguments must be counted, not guessed at."""
    provider = model_router.OpenAICompatible(
        model_router.PROVIDERS["numes"], "test-key", sleep=lambda _s: None
    )

    class _Response:
        status_code = 200
        headers: dict[str, str] = {}

        @staticmethod
        def json() -> dict[str, object]:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "function": {"name": "read_file", "arguments": "{not json"},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"total_tokens": 3},
            }

    result = provider._parse(_Response(), attempt=1)  # noqa: SLF001 - the parse path is the unit
    assert result.ok is True
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].arguments is None
    assert result.tool_calls[0].valid is False
    assert "JSONDecodeError" in result.tool_calls[0].argument_error


def test_the_workspace_refuses_a_stale_precondition_and_a_missing_text(tmp_path: Path) -> None:
    workspace = bakeoff_tools.Workspace.fresh(tmp_path / "ws")
    original = workspace.read_text("calc/core.py")
    assert original is not None

    stale = workspace.apply_patch("calc/core.py", "def add(a, b):", "def plus(a, b):", "0" * 64)
    assert stale.payload["error"] == "stale_precondition"
    assert stale.payload["current_sha"] == bakeoff_tools.sha256_text(original)

    missing = workspace.apply_patch(
        "calc/core.py", "def meean(values):", "def mean(values):", stale.payload["current_sha"]
    )
    assert missing.payload["error"] == "no_match"

    ambiguous = workspace.apply_patch(
        "calc/core.py", "    return", "    return ", stale.payload["current_sha"]
    )
    assert ambiguous.payload["error"] in ("ambiguous_match", "no_match")

    applied = workspace.apply_patch(
        "calc/core.py",
        "def add(a, b):",
        "def add(a, b):  # noqa: D103",
        stale.payload["current_sha"],
    )
    assert applied.payload["applied"] is True
    assert workspace.read_text("calc/core.py") != original


def test_the_workspace_tests_run_from_the_workspace(tmp_path: Path) -> None:
    workspace = bakeoff_tools.Workspace.fresh(tmp_path / "ws")
    result = workspace.run_tests()
    assert result.payload["exit_code"] == 0, result.payload
    assert "10 passed" in str(result.payload["summary"])


def test_paths_outside_the_workspace_are_refused(tmp_path: Path) -> None:
    workspace = bakeoff_tools.Workspace.fresh(tmp_path / "ws")
    for path in ("../outside.txt", "/etc/hostname"):
        assert workspace.read_file(path).payload["error"] == "no_such_file"
        assert workspace.list_dir(path).payload["error"] == "no_such_directory"


def test_wilson_interval_brackets_the_estimate() -> None:
    low, high = model_bakeoff.wilson(6, 12)
    assert 0.0 <= low < 0.5 < high <= 1.0
    low, high = model_bakeoff.wilson(0, 10)
    assert low == 0.0 and 0.0 < high < 0.35
    low, high = model_bakeoff.wilson(10, 10)
    assert 0.65 < low <= 1.0 and high == 1.0
    assert model_bakeoff.wilson(0, 0) == (0.0, 1.0)


def test_exact_argument_matching_normalises_documented_defaults() -> None:
    task = next(item for item in model_bakeoff.tasks_module.TASKS if item.id == "single-read-range")
    exact = [{"name": "read_file", "arguments": {"path": "calc/core.py", "start": 1, "end": 15}}]
    implicit_default = [{"name": "read_file", "arguments": {"path": "calc/core.py", "end": 15}}]
    wrong = [{"name": "read_file", "arguments": {"path": "calc/core.py", "start": 2, "end": 15}}]
    assert model_bakeoff._expected_matches(task, exact) is True  # noqa: SLF001
    assert model_bakeoff._expected_matches(task, implicit_default) is True  # noqa: SLF001
    assert model_bakeoff._expected_matches(task, wrong) is False  # noqa: SLF001


def test_every_task_declares_enough_to_score_it() -> None:
    seen = set()
    for task in model_bakeoff.tasks_module.TASKS:
        assert task.id not in seen, f"duplicate task id {task.id}"
        seen.add(task.id)
        assert task.category in {"single", "multi", "recovery", "discipline", "injection"}
        assert task.check.strip()
        assert task.budget >= 1
        if task.category == "single":
            assert task.expect_calls, task.id
            assert task.tests_must_pass is False, task.id
        if task.category == "injection":
            assert task.tests_unchanged, task.id
    assert len(model_bakeoff.tasks_module.STAGE_ONE) == 12
    assert len(model_bakeoff.tasks_module.TASKS) == 20


def test_redaction_strips_credentials_from_a_response_record() -> None:
    redacted = model_router.redact(
        {"headers": {"Authorization": "Bearer sk-secret"}, "nested": [{"api_key": "x"}]}
    )
    assert redacted["headers"]["Authorization"] == "<redacted>"
    assert redacted["nested"][0]["api_key"] == "<redacted>"
