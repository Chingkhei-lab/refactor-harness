"""The harness path, attacked: does the sandbox contain and record it?

Three behaviours a case can attempt that the sandbox must stop, and that the
harness must still *observe* rather than crash on:

  * writing outside its workspace (read-only root filesystem, no host mount)
  * opening a network connection (`network_mode=none`: no interfaces, no DNS)
  * forking without limit (`pids_limit`)

These need a real daemon, so they run in the `sandbox-hostile` CI job
(`ENGINE_SANDBOX_TESTS=1`) and skip elsewhere. The transport's bookkeeping —
copy-in, copy-out, byte-identical observations, a payload killed by its CPU
budget still yielding its begin marker — is covered without a daemon in
`tests/test_harness_sandbox_transport.py`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from harness import determinism
from harness.cases import Case, case_set_hash
from harness.runner import run_pass

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "canary"

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("ENGINE_SANDBOX_TESTS") != "1",
        reason="hostile suite needs the docker SDK + ENGINE_SANDBOX_TESTS=1",
    ),
    pytest.mark.skipif(sys.platform == "win32", reason="container tests run on Linux"),
]

CASES = [Case(id="0001-hostile", source="hostile", entry="escape", args=(0,))]


def _sandbox_pass(tmp_path: Path) -> object:
    import docker

    from harness.sandbox_transport import DockerExecutor

    checkout = tmp_path / "target"
    checkout.mkdir()
    (checkout / "hostile_target.py").write_bytes(
        (FIXTURE / "hostile_target.py").read_bytes()
    )
    return run_pass(
        python=sys.executable,
        checkout=checkout,
        shim_dir=determinism.write_shim(tmp_path / "shim"),
        src_dir=REPO / "src",
        target_paths=[checkout],
        cases=CASES,
        index=1,
        module="hostile_target",
        workdir=tmp_path / "work",
        expected_case_set_hash=case_set_hash(CASES),
        executor=DockerExecutor(docker.from_env(), "python:3.12-slim"),
    )


def test_a_hostile_target_is_contained_and_recorded(tmp_path: Path) -> None:
    result = _sandbox_pass(tmp_path)
    observations = list(result.observations)  # type: ignore[attr-defined]
    assert len(observations) == 1
    observation = observations[0]
    assert observation.outcome == "return", observation.exception

    outcome = observation.value_body()  # type: ignore[attr-defined]
    # Nothing succeeded. The exact exception type is the kernel's business.
    assert outcome["write"] != "wrote", outcome
    assert outcome["net"] != "connected", outcome
    assert outcome["fork"] != "forked", outcome
    # Evidence that the cgroup pids limit is what stopped it: some children were
    # created, and far fewer than the 200 the loop asked for.
    assert 0 < outcome["forks_created"] < 200, outcome
    # And the workspace is the container's tmpfs, not a host path.
    assert outcome["workdir"] == "/workspace"


def test_a_target_that_sigkills_itself_is_a_behaviour_not_a_resource_kill(
    tmp_path: Path,
) -> None:
    """137 with a readable zero oom count is the target's own doing.

    Inside the container the cgroup counter is readable, so the ladder decides:
    a self-kill is recorded as `ProcessSignal` and compared, which is what makes
    "old returned, new SIGKILLs itself" a difference rather than an excuse.
    """
    import docker

    from harness.sandbox_transport import DockerExecutor

    checkout = tmp_path / "target"
    checkout.mkdir()
    (checkout / "suicide.py").write_text(
        "import os, signal" + chr(10) + chr(10)
        + "def die(_x):" + chr(10)
        + "    os.kill(os.getpid(), signal.SIGKILL)" + chr(10),
        encoding="utf-8",
    )
    cases = [Case(id="0001-die", source="suicide", entry="die", args=(0,))]
    result = run_pass(
        python=sys.executable,
        checkout=checkout,
        shim_dir=determinism.write_shim(tmp_path / "shim"),
        src_dir=REPO / "src",
        target_paths=[checkout],
        cases=cases,
        index=1,
        module="suicide",
        workdir=tmp_path / "work",
        case_budget_seconds=60.0,
        expected_case_set_hash=case_set_hash(cases),
        executor=DockerExecutor(docker.from_env(), "python:3.12-slim"),
    )
    observation = result.by_case()["0001-die"]  # type: ignore[attr-defined]
    assert observation.outcome == "exception", observation.exception
    process = observation.exception["__process__"]
    assert process["kind"] == "ProcessSignal"
    assert process["signal_name"] == "SIGKILL"
    assert process["oom_counter"] == 0


def test_observations_survive_a_payload_killed_by_its_budget(tmp_path: Path) -> None:
    """A CPU kill must still leave the finished cases and the begin marker."""
    import docker

    from harness.sandbox_transport import DockerExecutor

    checkout = tmp_path / "target"
    checkout.mkdir()
    (checkout / "burn.py").write_text(
        "def fast(x):\n    return x + 1\n\n\n"
        "def burn(_x):\n    while True:\n        pass\n",
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
        workdir=tmp_path / "work",
        case_budget_seconds=60.0,
        cpu_budget_seconds=2.0,
        expected_case_set_hash=case_set_hash(cases),
        executor=DockerExecutor(docker.from_env(), "python:3.12-slim"),
    )
    by_case = result.by_case()  # type: ignore[attr-defined]
    assert set(by_case) == {case.id for case in cases}, "the pass must resume"
    assert by_case["0002-burn"].outcome == "unsupported"
    process = by_case["0002-burn"].exception["__process__"]
    assert process["kind"] == "CpuBudget"
    assert by_case["0003-fast"].outcome == "return"
