"""Crash-as-behaviour, projections, `expect`, and the hard-limit arithmetic.

The distinction these tests protect: a target that crashes is a *finding*, a
budget kill is *unsupported*, and only a child that dies before any begin marker
is a harness bug. Getting that wrong in either direction loses information — a
crash reported as "unsupported" hides a refactor that introduced an abort, and a
budget kill reported as "different" blames the target for the limits.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from harness import determinism
from harness.cases import Case, case_set_hash, load_cases
from harness.observation import OBSERVATION_SCHEMA_VERSION
from harness.projections import PROJECTIONS, UnknownProjection, get
from harness.rules import RuleError, apply_rules, load_rules, usage
from harness.runner import _hard_cpu_seconds, run_pass

CRASHER = '''
import os


def fine(n: int = 1) -> int:
    return n + 1


def crash() -> int:
    os._exit(7)


def signal_crash() -> int:
    os.kill(os.getpid(), 11)  # SIGSEGV where available
    return 0
'''


def _pass(tmp_path: Path, module: str, source: str, cases: list[Case], index: int = 1):
    checkout = tmp_path / f"target-{module}"
    checkout.mkdir(parents=True, exist_ok=True)
    (checkout / f"{module}.py").write_text(source, encoding="utf-8")
    return run_pass(
        python=sys.executable,
        checkout=checkout,
        shim_dir=determinism.write_shim(tmp_path / "shim"),
        src_dir=Path(__file__).resolve().parents[1] / "src",
        target_paths=[checkout],
        cases=cases,
        index=index,
        module=module,
        workdir=tmp_path / f"work-{module}-{index}",
        case_budget_seconds=30.0,
        expected_case_set_hash=case_set_hash(cases),
    )


def test_a_target_exit_is_an_observation_not_a_harness_bug(tmp_path: Path) -> None:
    cases = [
        Case(id="0001-fine", source="docs", entry="fine", args=(1,)),
        Case(id="0002-crash", source="grammar", entry="crash", args=()),
        Case(id="0003-fine", source="docs", entry="fine", args=(2,)),
    ]
    ran = _pass(tmp_path, "crasher", CRASHER, cases)
    by_case = ran.by_case()
    assert set(by_case) == {case.id for case in cases}, "the pass must continue"
    crash = by_case["0002-crash"]
    assert crash.outcome == "exception"
    process = crash.exception["__process__"]
    assert process["kind"] == "ProcessExit"
    assert process["exit_code"] == 7
    assert ran.restarts == 1


def test_two_crashes_with_different_status_are_different(tmp_path: Path) -> None:
    """The comparison sees the exit status, which is the whole point."""
    from harness.compare import DIFFERENT, compare

    cases = [Case(id="0001-crash", source="docs", entry="crash", args=())]
    left = _pass(tmp_path, "crasher_a", CRASHER, cases, 1)
    other_source = CRASHER.replace("os._exit(7)", "os._exit(9)")
    right = _pass(tmp_path, "crasher_b", other_source, cases, 2)
    verdict = compare(left.by_case()["0001-crash"], right.by_case()["0001-crash"])
    assert verdict.outcome == DIFFERENT
    assert verdict.path.startswith("$.exception.__process__")


def test_a_signal_death_is_recorded_with_its_signal(tmp_path: Path) -> None:
    if os.name == "nt":  # pragma: no cover - SIGSEGV via os.kill is POSIX-only
        pytest.skip("os.kill(SIGSEGV) is not available on Windows")
    cases = [Case(id="0001-signal", source="docs", entry="signal_crash", args=())]
    ran = _pass(tmp_path, "signaller", CRASHER, cases)
    observation = ran.by_case()["0001-signal"]
    process = observation.exception["__process__"]
    assert process["kind"] == "ProcessSignal"
    assert process["signal"] == 11
    assert process["signal_name"] == "SIGSEGV"


def test_the_hard_limit_covers_a_legitimate_pass() -> None:
    """hard = 2 * per_case * cases, floor 60: a healthy pass never reaches it."""
    assert _hard_cpu_seconds(10.0, 273) == 5460
    assert _hard_cpu_seconds(10.0, 5) == 100
    assert _hard_cpu_seconds(1.0, 1) == 60
    assert _hard_cpu_seconds(0.0, 273) == 3600, "no budget means the old backstop"


def test_projection_allowlist_refuses_anything_else() -> None:
    assert "evaluate_result" in PROJECTIONS
    with pytest.raises(UnknownProjection, match="not a declared projection"):
        get("__import__")


def test_a_case_file_with_an_unknown_projection_is_refused(tmp_path: Path) -> None:
    import json

    path = tmp_path / "cases.json"
    path.write_text(
        json.dumps(
            {
                "cases": [
                    {"id": "0001-x", "source": "docs", "entry": "parse",
                     "args": [], "projection": "run_this_code"}
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="not a declared projection"):
        load_cases(path)


def test_expect_stream_keeps_a_truncated_value_comparable(tmp_path: Path) -> None:
    """A stream is expected to be cut off; a finite case that is cut off is not."""
    source = '''
def stream():
    return (index for index in range(5000))


def finite():
    return (index for index in range(5000))
'''
    cases = [
        Case(id="0001-stream", source="docs", entry="stream", args=(), expect="stream"),
        Case(id="0002-finite", source="docs", entry="finite", args=(), expect="finite"),
    ]
    ran = _pass(tmp_path, "streams", source, cases)
    by_case = ran.by_case()
    assert by_case["0001-stream"].outcome == "return"
    assert by_case["0001-stream"].flags["truncated"] is True
    assert by_case["0002-finite"].outcome == "unsupported"
    assert by_case["0002-finite"].exception["__exception__"]["type"] == "UnobservableValue"
    assert by_case["0002-finite"].flags["limit"] == "items"


def test_observation_schema_version_is_in_every_observation() -> None:
    from harness.observation import Observation

    body = Observation(case_id="c", entry="e", inputs={}, outcome="return").body()
    assert body["observation_schema_version"] == OBSERVATION_SCHEMA_VERSION


def test_a_quoted_path_segment_can_name_a_key_with_a_dot(tmp_path: Path) -> None:
    path = tmp_path / "rules.json"
    import json

    path.write_text(
        json.dumps(
            {
                "target": "t",
                "case_set_hash": "e" * 64,
                "rules": [
                    {"id": "dotted", "path": '$.value["a.b"]', "type": "uuid",
                     "reason": "a key containing a dot can be named explicitly"}
                ],
            }
        ),
        encoding="utf-8",
    )
    rules = load_rules(path)
    left = {"value": {"a.b": "5eed0000-0000-0000-0000-000000000001"}}
    right = {"value": {"a.b": "5eed0000-0000-0000-0000-000000000002"}}
    applied = apply_rules(left, right, rules)
    assert applied.applied and applied.applied[0].path == '$.value["a.b"]'

    unquoted = tmp_path / "bad.json"
    unquoted.write_text(
        json.dumps({"target": "t", "case_set_hash": "e" * 64, "rules": [
            {"id": "bad", "path": "$.value[a.b]", "type": "uuid",
             "reason": "unquoted bracket segments must be refused"}]}),
        encoding="utf-8",
    )
    with pytest.raises(RuleError, match="must be quoted"):
        load_rules(unquoted)


def test_rule_usage_reports_how_close_a_rule_came(tmp_path: Path) -> None:
    path = tmp_path / "rules.json"
    import json

    path.write_text(
        json.dumps(
            {
                "target": "t",
                "case_set_hash": "e" * 64,
                "rules": [
                    {"id": "approx", "path": "$.value", "type": "float",
                     "tolerance": 1e-6, "reason": "float rounding is not behaviour"}
                ],
            }
        ),
        encoding="utf-8",
    )
    rules = load_rules(path)
    applied = apply_rules({"value": {"__float__": "1.0000001"}},
                          {"value": {"__float__": "1.0000002"}}, rules)
    stats = usage(rules, applied.applied)
    assert stats["approx"]["matched"] == 1
    assert 0.0 < stats["approx"]["max_ratio"] < 1.0, "under the tolerance, visibly"
    assert stats["approx"]["max_delta"] == pytest.approx(1e-7, rel=1e-3)


def test_an_import_failure_is_a_finding_for_every_case(tmp_path: Path) -> None:
    """A target module that will not import is a crash, not a harness bug."""
    from harness.compare import DIFFERENT, compare

    good = "def fine():\n    return 1\n"
    broken = "MISSING = UNDEFINED_NAME\n\n\ndef fine():\n    return 1\n"
    cases = [
        Case(id="0001-a", source="docs", entry="fine", args=()),
        Case(id="0002-b", source="docs", entry="fine", args=()),
    ]
    left = _pass(tmp_path, "importer", good, cases, 1)
    right = _pass(tmp_path, "importer", broken, cases, 2)
    assert set(right.by_case()) == {case.id for case in cases}
    for case in cases:
        observation = right.by_case()[case.id]
        assert observation.outcome == "exception"
        assert observation.exception["__import__"]["type"] == "NameError"
        verdict = compare(left.by_case()[case.id], observation)
        assert verdict.outcome == DIFFERENT


def test_a_shell_reported_signal_classifies_like_a_negative_returncode() -> None:
    """The sandbox reports 128+n; the host reports -n. Same classification.

    This is the bug the sandbox-hostile CI job found: a CPU kill inside the
    container arrives as exit 152, and without this mapping it was compared as
    "the target exited 152" instead of recorded as a budget.
    """
    from harness.runner import _death_observation

    case = Case(id="0001", source="x", entry="e", args=())
    for returncode, kind, signal_name in (
        (-24, "CpuBudget", "SIGXCPU"),
        (152, "CpuBudget", "SIGXCPU"),
        (-9, "ResourceKill", "SIGKILL"),
        (137, "ResourceKill", "SIGKILL"),
    ):
        observation = _death_observation(case, returncode, "")
        process = observation.exception["__process__"]
        assert process["kind"] == kind, (returncode, process)
        assert process["signal_name"] == signal_name, (returncode, process)
        assert observation.outcome == "unsupported"
