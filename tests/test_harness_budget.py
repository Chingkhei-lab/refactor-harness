"""The CPU budget: a case that loops in C is killed, attributed, and the pass goes on.

Skipped on Windows, where `resource` does not exist and the wall clock is the only
bound (the report says which budget was used). The C-level loop matters: a Python
loop would return to the interpreter and could be interrupted, while a regex
backtracking stays inside the C engine, which is where signal behaviour differs.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from harness import determinism
from harness.cases import Case, case_set_hash
from harness.runner import run_pass


def _has_rlimit() -> bool:
    try:
        import resource
    except ImportError:
        return False
    return hasattr(resource, "RLIMIT_CPU")


pytestmark = pytest.mark.skipif(
    not _has_rlimit(), reason="resource.setrlimit(RLIMIT_CPU) is POSIX-only"
)

MODULE = '''
import re


def fast(n: int = 1) -> int:
    return n * 2


def burn() -> bool:
    return re.match(r"(a+)+b", "a" * 40 + "c") is not None
'''


def test_a_cpu_bound_case_is_unsupported_and_the_pass_continues(tmp_path: Path) -> None:
    checkout = tmp_path / "target"
    checkout.mkdir()
    (checkout / "cpu_target.py").write_text(MODULE, encoding="utf-8")
    cases = [
        Case(id="0001-fast", source="docs", entry="fast", args=(1,)),
        Case(id="0002-burn", source="grammar", entry="burn", args=()),
        Case(id="0003-fast", source="docs", entry="fast", args=(2,)),
    ]
    result = run_pass(
        python=sys.executable,
        checkout=checkout,
        shim_dir=determinism.write_shim(tmp_path / "shim"),
        src_dir=Path(__file__).resolve().parents[1] / "src",
        target_paths=[checkout],
        cases=cases,
        index=1,
        module="cpu_target",
        workdir=tmp_path / "work",
        case_budget_seconds=60,
        cpu_budget_seconds=2,
        expected_case_set_hash=case_set_hash(cases),
    )
    by_case = result.by_case()
    assert set(by_case) == {case.id for case in cases}, "the pass must resume, not stop"
    assert by_case["0002-burn"].outcome == "unsupported"
    # Every child death is recorded in the same shape: `__process__` with a kind.
    # A budget kill is distinguished by `kind`, not by a different key.
    process = by_case["0002-burn"].exception["__process__"]
    assert process["kind"] == "CpuBudget"
    assert process["signal_name"] == "SIGXCPU"
    assert by_case["0001-fast"].outcome == "return"
    assert by_case["0003-fast"].outcome == "return"
