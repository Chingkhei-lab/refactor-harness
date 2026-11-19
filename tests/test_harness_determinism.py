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
