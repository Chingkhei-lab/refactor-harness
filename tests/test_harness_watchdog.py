"""A hung case is bounded by a per-case watchdog, on every platform.

Without RLIMIT_CPU (Windows) the only bound is the wall clock, and a pass-wide
timeout would let one hanging case hold the child for hours. The watchdog looks at
progress — a begin marker or an observation per case — and kills a child that has
made none for the case budget.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from harness import determinism
from harness.cases import Case, case_set_hash
from harness.runner import run_pass

MODULE = '''
import re


def fine(n: int = 1) -> int:
    return n + 1


def hang() -> bool:
    return re.match(r"(a+)+b", "a" * 40 + "c") is not None
'''


def test_a_hung_case_is_bounded_and_the_pass_continues(tmp_path: Path) -> None:
    checkout = tmp_path / "target"
    checkout.mkdir()
    (checkout / "hangmod.py").write_text(MODULE, encoding="utf-8")
    cases = [
        Case(id="0001-fine", source="docs", entry="fine", args=(1,)),
        Case(id="0002-hang", source="grammar", entry="hang", args=()),
        Case(id="0003-fine", source="docs", entry="fine", args=(2,)),
    ]
    started = time.monotonic()
    result = run_pass(
        python=sys.executable,
        checkout=checkout,
        shim_dir=determinism.write_shim(tmp_path / "shim"),
        src_dir=Path(__file__).resolve().parents[1] / "src",
        target_paths=[checkout],
        cases=cases,
        index=1,
        module="hangmod",
        workdir=tmp_path / "work",
        case_budget_seconds=3.0,
        expected_case_set_hash=case_set_hash(cases),
    )
    elapsed = time.monotonic() - started
    by_case = result.by_case()
    assert set(by_case) == {case.id for case in cases}
    assert by_case["0002-hang"].outcome == "unsupported"
    assert by_case["0002-hang"].exception["__exception__"]["type"] == "CaseTimeout"
    assert by_case["0001-fine"].outcome == "return"
    assert by_case["0003-fine"].outcome == "return"
    assert elapsed < 60, f"the watchdog must bound the hang, took {elapsed:.0f}s"
