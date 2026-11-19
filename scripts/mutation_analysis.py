"""Classify mutation survivors: not covered, covered but not observed, equivalent.

The three-way split is the part of a mutation report that is actually usable. A
survivor on a line the case set never executes is a coverage statement; a survivor
on a covered line is a case-set or harness weakness; a survivor that changes
nothing observable is an equivalent mutant and not a defect at all.

This script does the mechanical part (line coverage of the mutated site) and
merges in the manual verdicts, which live in `docs/results/mutation-verdicts.json`
so a reader can check them against the source.

Usage:

    python scripts/mutation_analysis.py
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from harness import determinism  # noqa: E402
from harness.cases import case_set_hash, load_cases  # noqa: E402
from harness.runner import run_pass  # noqa: E402

VERDICT_KINDS = ("not-covered", "covered-not-observed", "equivalent", "unjudged")


def _coverage_lines(cases: list[Any], work: Path, shim: Path) -> set[int]:
    run_pass(
        python=sys.executable,
        checkout=(_REPO_ROOT / ".harness" / "target").resolve(),
        shim_dir=shim,
        src_dir=_REPO_ROOT / "src",
        target_paths=[(_REPO_ROOT / ".harness" / "target").resolve()],
        cases=cases,
        index=999,
        module="parse",
        workdir=work,
        coverage=True,
        case_budget_seconds=30.0,
        expected_case_set_hash=case_set_hash(cases),
    )
    json_path = work / "coverage.json"
    subprocess.run(
        [sys.executable, "-m", "coverage", "json", "-o", str(json_path),
         f"--data-file={work / 'coverage.data'}"],
        cwd=str(_REPO_ROOT), capture_output=True, text=True,
    )
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    executed: set[int] = set()
    for name, data in payload["files"].items():
        if name.endswith("parse" + "\\__init__.py") or name.endswith("parse/__init__.py"):
            executed |= set(data.get("executed_lines") or [])
    return executed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verdicts", default="docs/results/mutation-verdicts.json")
    parser.add_argument("--out", default="docs/results")
    args = parser.parse_args()

    payload = json.loads(Path(".harness/mutation/results.json").read_text(encoding="utf-8"))
    plan, results = payload["plan"], payload["results"]
    cases = load_cases(_REPO_ROOT / "harness" / "cases" / "parse.json")
    shim = determinism.write_shim(_REPO_ROOT / ".harness" / "shim")
    work = _REPO_ROOT / ".harness" / "mutation" / "coverage"
    work.mkdir(parents=True, exist_ok=True)
    executed = _coverage_lines(cases, work, shim)

    verdicts_path = _REPO_ROOT / args.verdicts
    manual: dict[str, str] = {}
    if verdicts_path.exists():
        manual = json.loads(verdicts_path.read_text(encoding="utf-8")).get("verdicts", {})

    source = (_REPO_ROOT / ".harness" / "target" / "parse" / "__init__.py").read_text(
        encoding="utf-8"
    ).splitlines()

    survivors = [item for item in results if not item["harness_killed"]]
    rows: list[dict[str, Any]] = []
    for item in survivors:
        covered = item["line"] in executed
        verdict = manual.get(item["site"], "")
        if not verdict:
            verdict = "covered-not-observed" if covered else "not-covered"
        rows.append(
            {
                **item,
                "covered": covered,
                "source": source[item["line"] - 1].strip() if item["line"] else "",
                "verdict": verdict,
                "hand_judged": item["site"] in manual,
            }
        )

    split = Counter(row["verdict"] for row in rows)
    judged = {row["site"]: row["verdict"] for row in rows if row["hand_judged"]}
    report = {
        "plan": plan,
        "coverage_checked": len(executed),
        "survivors": len(rows),
        "split": dict(split),
        "executed_survivors": sum(1 for row in rows if row["covered"]),
        "rows": rows,
        "manual_verdicts": judged,
    }
    out_dir = _REPO_ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "mutation-survivors.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(f"survivors: {len(rows)} | split: {dict(split)}")
    print(f"covered survivors needing a verdict: "
          f"{sum(1 for row in rows if row['covered'] and not row['hand_judged'])}")
    for row in rows:
        if row["covered"]:
            print(f"  {row['site']:26} line {row['line']:5}  {row['source'][:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
