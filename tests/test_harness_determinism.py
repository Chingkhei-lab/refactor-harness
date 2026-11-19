"""The shim proves itself: two runs of the same probe are byte-identical.

A shim nobody tests is a hope. These tests run a real child interpreter that
prints the clock, a random number, a UUID and a hash, twice, and compare bytes;
then they run the same probe WITHOUT the shim to show the controls are doing
something. The last test pins the environment allowlist, because a leaked
parent variable would be a difference that has nothing to do with the target.
"""

from __future__ import annotations

import sys
from pathlib import Path

from harness import determinism


def test_probe_is_byte_identical_across_runs(tmp_path: Path) -> None:
    shim_dir = determinism.write_shim(tmp_path / "shim")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    first = determinism.run_probe(sys.executable, checkout, shim_dir, [])
    second = determinism.run_probe(sys.executable, checkout, shim_dir, [])
    assert first["returncode"] == 0, first["stderr"]
    assert first["stdout"] == second["stdout"], "the shim did not make the run reproducible"
    assert "clock_frozen" in first["controls"]
    assert "random_seeded" in first["controls"]
    assert "uuid_frozen" in first["controls"]
    assert "hash_seed_0" in first["controls"]
    assert str(int(determinism.FROZEN_EPOCH)) in first["stdout"]


def test_controls_actually_change_behaviour(tmp_path: Path) -> None:
    shim_dir = determinism.write_shim(tmp_path / "shim")
    bare_dir = tmp_path / "bare"
    bare_dir.mkdir()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    shimmed = determinism.run_probe(sys.executable, checkout, shim_dir, [])
    unshimmed = determinism.run_probe(sys.executable, checkout, bare_dir, [])
    assert shimmed["stdout"] != unshimmed["stdout"], "the probe is insensitive to the shim"
    assert not unshimmed["controls"]


def test_child_env_is_an_allowlist(tmp_path: Path) -> None:
    env = determinism.child_env(tmp_path, tmp_path / "shim", [tmp_path / "target"])
    assert env["PYTHONHASHSEED"] == "0"
    assert env["TZ"] == "UTC"
    assert env["LC_ALL"] == "C.UTF-8"
    assert "DATABASE_URL" not in env
    assert "ENGINE_DEMO_DATABASE_URL" not in env
    assert all(name.upper() != "AWS_SECRET_ACCESS_KEY" for name in env)


def test_frozen_epoch_is_a_fixed_instant() -> None:
    assert determinism.FROZEN_EPOCH == 1_700_000_000.0


_RESET_PROBE = '''
import random, uuid
import sitecustomize
for i in range(3):
    print(i, random.random(), uuid.uuid4())
    sitecustomize.reset_controls()
'''


def _run_script(tmp_path: Path, script: str, *, reset_script: str | None) -> str:
    import subprocess

    shim = tmp_path / "shim"
    shim.mkdir(parents=True, exist_ok=True)
    source = reset_script or determinism.SHIM_SOURCE
    (shim / "sitecustomize.py").write_text(source, encoding="utf-8")
    env = determinism.child_env(tmp_path, shim, [])
    return subprocess.run(
        [sys.executable, "-c", script], cwd=str(tmp_path), env=env,
        capture_output=True, text=True, timeout=60,
    ).stdout


def test_per_case_reset_makes_values_independent_of_position(tmp_path: Path) -> None:
    """The coupling per-case reset exists to remove.

    `random` and the uuid counter are process state. Without a reset, the value a
    case sees depends on how many draws the cases before it made — so a refactor
    that changes one case's draw count shifts every later case and looks like a
    flood of behaviour changes.
    """
    with_reset = _run_script(tmp_path / "a", _RESET_PROBE, reset_script=None)
    first = [line.split(" ", 1)[1] for line in with_reset.splitlines()]
    assert first[0] == first[1] == first[2], "every case must start from the same state"

    no_reset = determinism.SHIM_SOURCE.replace(
        '    _random.seed(0)\n    _counter["n"] = 0\n', "    pass\n"
    )
    without = _run_script(tmp_path / "b", _RESET_PROBE, reset_script=no_reset)
    values = [line.split(" ", 1)[1] for line in without.splitlines()]
    assert len(set(values)) > 1, "without reset the values must drift with position"
