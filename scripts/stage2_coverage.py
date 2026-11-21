"""Stage-2 coverage: does the case set reach the code the migration touches?

A CLI case runs the target as a grandchild process, so ordinary coverage of the
harness child never sees the target's lines. The hook is
`COVERAGE_PROCESS_START`, installed in the shim's `sitecustomize` (which is
already on `PYTHONPATH`) and passed through the case environment; every
subprocess then writes its own data file and `coverage combine` merges them.

Two numbers are reported, because one is not enough to answer the question:

* **whole-module coverage** — how much of `pycodestyle.py` the cases reach;
* **the migration's own surface** — the lines of `get_parser`, `_option_kinds`,
  `read_config` and `process_options`, which is what the change under test
  actually rewrites. A case set can be 30 % of the module and still cover the
  argument handling completely, and that is the case that matters.

Plausibility checks, so a coverage number cannot be wrong quietly:

1. with **one** case the numbers must be strictly lower than with all of them;
2. an empty case set must report 0 % for the target, not a crash;
3. the function that runs the CLI (`_main`) must appear, or the hook is not
   reaching the subprocess at all.

    python scripts/stage2_coverage.py --target .harness/oracle/pycodestyle
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from harness import determinism  # noqa: E402
from harness.cases import Case, case_set_hash, load_cases  # noqa: E402
from harness.runner import run_pass  # noqa: E402

SURFACE = ("get_parser", "_option_kinds", "read_config", "process_options", "_main")


def _coverage(target: Path, data_file: Path, module: str) -> dict[str, Any]:
    json_path = data_file.with_suffix(".json")
    subprocess.run(
        [sys.executable, "-m", "coverage", "json", "-o", str(json_path),
         f"--data-file={data_file}"],
        cwd=str(REPO), capture_output=True, text=True,
    )
    payload = json.loads(json_path.read_text(encoding="utf-8")) if json_path.exists() else {}
    totals = payload.get("totals", {})
    result: dict[str, Any] = {
        "statements": totals.get("num_statements", 0),
        "covered_statements": totals.get("covered_lines", 0),
        "branches": totals.get("num_branches", 0),
        "covered_branches": totals.get("covered_branches", 0),
        "line_percent": 0.0,
        "branch_percent": 0.0,
        "combined_percent": 0.0,
        "never_entered_functions": [],
        "partially_covered_functions": [],
        "surface": {},
        "function_count": 0,
    }
    if result["statements"]:
        result["line_percent"] = round(
            100.0 * result["covered_statements"] / result["statements"], 2
        )
    if result["branches"]:
        result["branch_percent"] = round(
            100.0 * result["covered_branches"] / result["branches"], 2
        )
    if result["statements"] + result["branches"]:
        result["combined_percent"] = round(
            100.0 * (result["covered_statements"] + result["covered_branches"])
            / (result["statements"] + result["branches"]),
            2,
        )

    never: list[dict[str, Any]] = []
    partial: list[dict[str, Any]] = []
    surface: dict[str, Any] = {}
    count = 0
    for path, file_data in payload.get("files", {}).items():
        for name, function in (file_data.get("functions") or {}).items():
            count += 1
            missing = function.get("missing_lines") or []
            executed = function.get("executed_lines") or []
            entry = {
                "function": f"{Path(path).name}::{name}",
                "start_line": function.get("start_line"),
                "executed_lines": len(executed),
                "missing_lines": len(missing),
            }
            if missing and executed:
                partial.append(entry)
            elif missing and not executed:
                never.append(entry)
            if name in SURFACE:
                surface[name] = {
                    "start_line": function.get("start_line"),
                    "executed": len(executed),
                    "missing": len(missing),
                    "missing_lines": missing[:20],
                }
    result["never_entered_functions"] = sorted(never, key=lambda item: item["function"])
    result["partially_covered_functions"] = sorted(partial, key=lambda item: item["function"])
    result["function_count"] = count
    result["surface"] = surface
    return result


def measure(target: Path, cases: list[Case], workdir: Path) -> dict[str, Any]:
    shim = determinism.write_shim(workdir / "shim")
    data = workdir / "coverage.data"
    run_pass(
        python=sys.executable,
        checkout=target,
        shim_dir=shim,
        src_dir=REPO / "src",
        target_paths=[target],
        cases=cases,
        index=1,
        module="pycodestyle",
        workdir=workdir,
        coverage=True,
        case_budget_seconds=30.0,
        expected_case_set_hash=case_set_hash(cases),
    )
    # Parallel data files are `<data_file>.<host>.<pid>.<random>`. `combine`
    # matches on the data_file PREFIX, so it must be given the same path the
    # subprocesses used — pointing it at `merged.data` finds no inputs at all and
    # silently produces nothing, which is how a single subprocess file ended up
    # being read as if it were the whole run.
    raw = sorted(path for path in workdir.glob("coverage.data.*") if path.suffix != ".json")
    if raw:
        combined = subprocess.run(
            [sys.executable, "-m", "coverage", "combine", f"--data-file={data}", str(workdir)],
            cwd=str(workdir), capture_output=True, text=True,
        )
        if not data.exists():
            raise RuntimeError(f"coverage combine produced no data: {combined.stderr[-300:]}")
    return _coverage(target, data, "pycodestyle")


def executed_lines(target: Path, data_file: Path, module: str) -> dict[str, Any]:
    """The executed line numbers of the module under test, for the mutation audit.

    A mutation whose line no case executes cannot be observed, so a survivor
    there says nothing about the case set and a *kill* there says the evaluator
    saw something other than the mutated line. That distinction is only possible
    with the line list, not with a percentage.
    """
    json_path = data_file.with_suffix(".lines.json")
    subprocess.run(
        [sys.executable, "-m", "coverage", "json", "-o", str(json_path),
         f"--data-file={data_file}"],
        cwd=str(REPO), capture_output=True, text=True,
    )
    payload = json.loads(json_path.read_text(encoding="utf-8")) if json_path.exists() else {}
    for path, file_data in payload.get("files", {}).items():
        if Path(path).name == f"{module}.py":
            return {
                "module": f"{module}.py",
                "executed": sorted(file_data.get("executed_lines") or []),
                "missing": sorted(file_data.get("missing_lines") or []),
            }
    return {"module": f"{module}.py", "executed": [], "missing": []}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default=".harness/oracle/pycodestyle")
    parser.add_argument("--cases", default="harness/cases/pycodestyle-cli.json")
    parser.add_argument("--out", default="docs/results/stage2-coverage-20261120.json")
    parser.add_argument("--lines-out", default="")
    args = parser.parse_args(argv)
    target = (REPO / args.target).resolve()
    cases = load_cases(REPO / args.cases)
    root = REPO / ".harness" / "stage2-coverage"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)

    full = measure(target, cases, root / "full")
    one = measure(target, cases[:1], root / "one")
    empty = measure(target, [], root / "empty")

    if args.lines_out:
        lines = executed_lines(target, root / "full" / "coverage.data", "pycodestyle")
        (REPO / args.lines_out).write_text(json.dumps(lines, indent=1) + chr(10), encoding="utf-8")
        print(
            f"executed lines: {len(lines['executed'])} of "
            f"{len(lines['executed']) + len(lines['missing'])}"
        )
    report = {
        "target": "pycodestyle 2.15.0",
        "case_set_hash": case_set_hash(cases),
        "case_count": len(cases),
        "full": full,
        "one_case": one,
        "empty": empty,
        "checks": {
            "one_case_is_lower": one["combined_percent"] < full["combined_percent"],
            "empty_is_zero": empty["covered_statements"] == 0,
            "subprocess_reached": "main" in full["surface"] or "_main" in full["surface"],
        },
    }
    (REPO / args.out).write_text(
        json.dumps(report, indent=2, sort_keys=True) + chr(10),
        encoding="utf-8",
    )
    print(
        f"cases {len(cases)}: lines {full['covered_statements']}/{full['statements']} "
        f"({full['line_percent']}%), branches {full['covered_branches']}/{full['branches']} "
        f"({full['branch_percent']}%), combined {full['combined_percent']}%, "
        f"never-entered functions {len(full['never_entered_functions'])}/{full['function_count']}"
    )
    print(f"1 case: {one['combined_percent']}% | 0 cases: {empty['covered_statements']} statements")
    for name, stats in sorted(full["surface"].items()):
        print(f"  {name}: lines {stats['executed']}/{stats['executed'] + stats['missing']}"
              f" missing {stats['missing_lines']}")
    print("checks", report["checks"])
    checks = report["checks"]
    assert isinstance(checks, dict)
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
