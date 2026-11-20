"""The sandbox transport's bookkeeping, without needing a daemon.

`LocalExecutor` has the same copy-in/copy-out shape as the container path — a
fresh directory per run instead of a tmpfs, and the host interpreter — so it can
prove the parts that are easy to get wrong: the workspace layout inside the
tmpfs, the child's environment, the observations coming back out, and a payload
that dies still leaving its begin marker behind.

What it cannot prove is containment. That is `test_harness_sandbox_hostile.py`,
which needs a real daemon and runs in the `sandbox-hostile` CI job.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from harness import determinism
from harness.cases import Case, case_set_hash, load_cases
from harness.runner import run_pass
from harness.sandbox_transport import LocalExecutor, sandbox_env, workspace_files

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "canary"

CASES = [
    Case(id="0001-absolute-negative", source="canary", entry="absolute", args=(-3,)),
    Case(id="0002-absolute-positive", source="canary", entry="absolute", args=(3,)),
    Case(id="0003-label-negative", source="canary", entry="label", args=(-1,)),
]


def _files(tmp_path: Path) -> dict[str, bytes]:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "canary.py").write_bytes((FIXTURE / "canary.py").read_bytes())
    return workspace_files(
        checkout=checkout,
        shim_dir=determinism.write_shim(tmp_path / "shim"),
        src_dir=REPO / "src",
        plan=b'{"module": "canary", "cases": []}',
    )


def test_the_workspace_carries_no_host_path(tmp_path: Path) -> None:
    files = _files(tmp_path)
    assert "checkout/canary.py" in files
    assert "src/harness/child.py" in files
    assert any(name.startswith("shim/sitecustomize") for name in files)
    assert "plan.json" in files
    # Nothing may reference the directory it was built from: the container has
    # no host paths at all, and a copy-in that carries one would fail there.
    root = str(tmp_path).replace("\\", "/")
    leaked = [name for name in files if root in name]
    assert not leaked
    assert not [name for name in files if "__pycache__" in name]


def test_the_child_environment_is_an_allowlist(tmp_path: Path) -> None:
    env = sandbox_env(
        {
            "PYTHONPATH": "/host/path",
            "HARNESS_CWD_FIXED": "1",
            "HARNESS_ENV_SCRUBBED": "1",
            "PYTHONHASHSEED": "0",
            "TZ": "UTC",
            "LC_ALL": "C.UTF-8",
            "AWS_SECRET_ACCESS_KEY": "must-not-leak",
        }
    )
    assert env["PYTHONPATH"] == "/workspace/shim:/workspace/src:/workspace/checkout"
    assert env["HARNESS_CWD_FIXED"] == "1"
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "/host/path" not in env["PYTHONPATH"]


def test_a_sandboxed_pass_matches_the_direct_pass_byte_for_byte(tmp_path: Path) -> None:
    shim = determinism.write_shim(tmp_path / "shim")
    common = dict(
        python=sys.executable,
        checkout=FIXTURE,
        shim_dir=shim,
        src_dir=REPO / "src",
        target_paths=[FIXTURE],
        cases=CASES,
        index=1,
        module="canary",
        expected_case_set_hash=case_set_hash(CASES),
    )
    direct = run_pass(workdir=tmp_path / "direct", **common)
    sandboxed = run_pass(
        workdir=tmp_path / "sandboxed",
        executor=LocalExecutor(tmp_path / "local"),
        **common,
    )
    assert [o.to_json() for o in direct.observations] == [
        o.to_json() for o in sandboxed.observations
    ]


def test_the_case_set_from_the_repo_survives_the_round_trip(tmp_path: Path) -> None:
    """The real case set, not a toy: same bytes through copy-in and copy-out."""
    cases = load_cases(REPO / "harness" / "cases" / "parse.json")[:24]
    target = REPO / ".harness" / "target"
    if not target.exists():
        pytest.skip("target not fetched; CI runs scripts/fetch_target.py first")
    shim = determinism.write_shim(tmp_path / "shim")
    common = dict(
        python=sys.executable,
        checkout=target,
        shim_dir=shim,
        src_dir=REPO / "src",
        target_paths=[target],
        cases=cases,
        index=1,
        expected_case_set_hash=case_set_hash(cases),
    )
    direct = run_pass(workdir=tmp_path / "d", **common)
    sandboxed = run_pass(
        workdir=tmp_path / "s", executor=LocalExecutor(tmp_path / "l"), **common
    )
    assert [o.to_json() for o in direct.observations] == [
        o.to_json() for o in sandboxed.observations
    ]


@pytest.mark.skipif(
    sys.platform == "win32", reason="no RLIMIT_CPU on Windows: nothing to kill with"
)
def test_a_payload_killed_by_its_budget_still_returns_its_observations(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "target"
    checkout.mkdir()
    (checkout / "burn.py").write_text(
        "def fast(x):\n    return x + 1\n\n\ndef burn(_x):\n    while True:\n        pass\n",
        encoding="utf-8",
    )
    cases = [
        Case(id="0001-fast", source="burn", entry="fast", args=(1,)),
        Case(id="0002-burn", source="burn", entry="burn", args=(0,)),
        Case(id="0003-fast", source="burn", entry="fast", args=(2,)),
    ]
    result = run_pass(
        python=sys.executable,
        checkout=checkout,
        shim_dir=determinism.write_shim(tmp_path / "shim"),
        src_dir=REPO / "src",
        target_paths=[checkout],
        cases=cases,
        index=1,
        module="burn",
        workdir=tmp_path / "w",
        case_budget_seconds=60.0,
        cpu_budget_seconds=2.0,
        expected_case_set_hash=case_set_hash(cases),
        executor=LocalExecutor(tmp_path / "local"),
    )
    by_case = result.by_case()
    assert set(by_case) == {case.id for case in cases}
    assert by_case["0002-burn"].outcome == "unsupported"
    assert by_case["0002-burn"].exception["__process__"]["kind"] == "CpuBudget"
    assert by_case["0003-fast"].outcome == "return"


def test_a_child_that_cannot_start_is_reported_not_swallowed(tmp_path: Path) -> None:
    """A plan whose module does not exist still yields one outcome per case.

    The transport must not turn a child that never ran into a silent gap: the
    pass either records the failure per case or raises, never both-empty.
    """
    checkout = tmp_path / "target"
    checkout.mkdir()
    (checkout / "placeholder.py").write_text("X = 1" + chr(10), encoding="utf-8")
    cases = [Case(id="0001", source="x", entry="anything", args=())]
    result = run_pass(
        python=sys.executable,
        checkout=checkout,
        shim_dir=determinism.write_shim(tmp_path / "shim"),
        src_dir=REPO / "src",
        target_paths=[checkout],
        cases=cases,
        index=1,
        module="module_that_does_not_exist",
        workdir=tmp_path / "w",
        expected_case_set_hash=case_set_hash(cases),
        executor=LocalExecutor(tmp_path / "local"),
    )
    by_case = result.by_case()
    assert set(by_case) == {"0001"}
    assert by_case["0001"].outcome in {"exception", "unsupported"}


def test_the_local_executor_keeps_each_run_in_its_own_directory(
    tmp_path: Path,
) -> None:
    executor = LocalExecutor(tmp_path / "local")
    first = executor.run(
        argv=[sys.executable, "-c", "print('one')"],
        env={},
        files={"a.txt": b"1"},
        limits=__import__("engine.sandbox", fromlist=["SandboxLimits"]).SandboxLimits(),
        image="unused",
        job_id="harness-1",
    )
    second = executor.run(
        argv=[sys.executable, "-c", "print('two')"],
        env={},
        files={"b.txt": b"2"},
        limits=__import__("engine.sandbox", fromlist=["SandboxLimits"]).SandboxLimits(),
        image="unused",
        job_id="harness-2",
    )
    assert first.exit_code == 0
    assert "a.txt" in first.files and "a.txt" not in second.files
    assert os.path.isdir(tmp_path / "local")
