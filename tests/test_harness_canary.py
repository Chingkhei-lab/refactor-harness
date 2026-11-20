"""Canaries for the evaluator itself: an obvious bug must die, a no-op must live.

The polarity bug in the 12a mutation runner survived because nothing tested the
runner — only its parts. These two canaries are the smallest possible end-to-end
check of the machinery that produces every number in the report:

  * a mutant that obviously breaks the target must come out **killed** by both
    oracles (the case set and the upstream tests);
  * a mutant that changes a comment only must come out **survived** by both.

Both run on every CI build, on a vendored two-function fixture, so they need no
network, no Docker and no `.harness/` directory.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path
from types import ModuleType

from harness import determinism
from harness.cases import Case, case_set_hash
from harness.compare import compare_sets
from harness.runner import run_pass

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "canary"
# The suite file is named `suite_canary.py`, not `test_canary.py`, so the
# repository-wide run does not collect it: it needs `canary` importable, and it
# is passed to pytest explicitly by the oracle.
SUITE = FIXTURE / "suite_canary.py"

CASES = [
    Case(id="0001-absolute-negative", source="canary", entry="absolute", args=(-3,)),
    Case(id="0002-absolute-positive", source="canary", entry="absolute", args=(3,)),
    Case(id="0003-label-negative", source="canary", entry="label", args=(-1,)),
    Case(id="0004-label-zero", source="canary", entry="label", args=(0,)),
    Case(id="0005-label-positive", source="canary", entry="label", args=(1,)),
]


def _mutate_script() -> ModuleType:
    """`scripts/` is not a package: load the runner whose polarity was wrong."""
    path = REPO / "scripts" / "mutate.py"
    spec = importlib.util.spec_from_file_location("mutate_script_canary", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _checkout(tmp_path: Path, *, broken: bool, comment: bool) -> Path:
    checkout = tmp_path / ("broken" if broken else "clean")
    shutil.copytree(FIXTURE, checkout)
    source = (checkout / "canary.py").read_text(encoding="utf-8")
    if broken:
        source = source.replace("        return -value", "        return +value")
    if comment:
        head = chr(10) + '"""A two-function target'
        source = source.replace(
            '"""A two-function target',
            "# a comment, nothing more" + head,
        )
    (checkout / "canary.py").write_text(source, encoding="utf-8")
    return checkout


def _harness_killed(tmp_path: Path, checkout: Path) -> bool:
    """Baseline on a pristine copy of the fixture, then on the variant."""
    shim = determinism.write_shim(tmp_path / "shim")
    clean = tmp_path / "pristine"
    if not clean.exists():
        shutil.copytree(FIXTURE, clean)
    base = run_pass(
        python=sys.executable,
        checkout=clean,
        shim_dir=shim,
        src_dir=REPO / "src",
        target_paths=[checkout],
        cases=CASES,
        index=1,
        module="canary",
        workdir=tmp_path / "work-baseline",
        expected_case_set_hash=case_set_hash(CASES),
    )
    changed = run_pass(
        python=sys.executable,
        checkout=checkout,
        shim_dir=shim,
        src_dir=REPO / "src",
        target_paths=[checkout],
        cases=CASES,
        index=2,
        module="canary",
        workdir=tmp_path / "work-variant",
        expected_case_set_hash=case_set_hash(CASES),
    )
    verdicts = compare_sets(base.by_case(), changed.by_case())
    return any(verdict.report_class == "different" for verdict in verdicts.values())


def test_an_obviously_broken_mutant_is_killed_by_both_oracles(tmp_path: Path) -> None:
    module = _mutate_script()
    checkout = _checkout(tmp_path, broken=True, comment=False)
    tests_killed, note = module._run_tests(FIXTURE / "suite_canary.py", checkout)
    assert tests_killed is True, note
    assert _harness_killed(tmp_path, checkout) is True


def test_a_comment_only_mutant_survives_both_oracles(tmp_path: Path) -> None:
    module = _mutate_script()
    checkout = _checkout(tmp_path, broken=False, comment=True)
    tests_killed, note = module._run_tests(FIXTURE / "suite_canary.py", checkout)
    assert tests_killed is False, note
    assert _harness_killed(tmp_path, checkout) is False


def test_the_oracle_is_not_inverted() -> None:
    """A passing suite must read as "not killed" — the 12a bug, pinned."""
    module = _mutate_script()
    killed, note = module._run_tests(FIXTURE / "suite_canary.py", FIXTURE)
    assert killed is False, f"an unmutated tree was reported killed: {note}"
    assert "passed" in note
