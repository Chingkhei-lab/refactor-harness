"""Audit a mutation run: why was each mutant killed, and was the kill earned?

A mutation score is a claim about the evaluator as much as about the case set.
The first stage-2 run reported 100/100 kills for both oracles, and the reason was
in the data: 77 of the 100 mutants differed on the same two cases, both of which
print a traceback naming the target's own file and line. The mutator re-printed
the whole module, so every mutant moved every line number. Nothing about that is
a kill.

This script answers the three questions that separate a score from a result, one
mutant at a time:

* **Was the mutated line executed by any case?** A kill on a line nothing runs is
  a kill the case could not have made; it means the evaluator saw something else
  — a line number, a path, a snapshot.
* **How many cases differ, and which?** A mutant that differs on nearly every
  case has broken the program, not changed a behaviour; a mutant that differs on
  one case is a case that noticed something specific.
* **Which tests fail, and do they test behaviour?** `test_all.py::test_own_dog_food`
  runs the tool over its own source: it fails on a formatting change, so a kill by
  that test alone is cosmetic and is counted separately.

    python scripts/mutation_audit.py \
        --results docs/results/mutation-pycodestyle-20261121.json \
        --lines .harness/stage2-executed-lines.json \
        --out docs/results/mutation-audit-pycodestyle-20261121.json
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]

# The upstream suite lints its own source; a mutant that only violates style is
# killed by that and by nothing else.
COSMETIC_TESTS = ("test_all.py::test_own_dog_food",)


def statement_spans(source: str) -> dict[int, tuple[int, int]]:
    """For every line, the narrowest statement span that contains it.

    Coverage reports *statement* lines only: the continuation lines of a
    multi-line `if` are not in `executed_lines`, so a mutation on one of them
    looks like a mutation on a line nothing runs. Nine of the stage-2 sample sit
    on continuation lines, and calling them unexecuted would have produced a
    finding about the case set that was really a finding about `coverage`'s line
    model. A line counts as reached when any line of its statement is.
    """
    import ast

    tree = ast.parse(source)
    spans: dict[int, tuple[int, int]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt):
            continue
        span = (node.lineno, node.end_lineno or node.lineno)
        for line in range(span[0], span[1] + 1):
            current = spans.get(line)
            if current is None or span[1] - span[0] < current[1] - current[0]:
                spans[line] = span
    return spans


def _reached(line: int, spans: dict[int, tuple[int, int]], executed: set[int]) -> bool:
    start, end = spans.get(line, (line, line))
    return any(number in executed for number in range(start, end + 1))


def _is_cosmetic(failures: list[str]) -> bool:
    if not failures:
        return False
    normalised = [failure.replace("\\", "/") for failure in failures]
    return all(any(test in failure for test in COSMETIC_TESTS) for failure in normalised)


def classify(
    row: dict[str, Any],
    reached: bool,
    case_count: int,
) -> str:
    """One label per mutant, chosen so the labels cannot overlap."""
    if row.get("error"):
        return "harness-error"
    if row.get("harness_killed"):
        if not reached:
            return "killed-on-a-line-nothing-runs"
        if row["differing_count"] >= max(3, case_count // 2):
            return "killed-on-most-cases"
        return "killed-on-some-cases"
    if row.get("tests_killed"):
        if _is_cosmetic((row.get("tests_detail") or {}).get("failures") or []):
            return "survived-harness-test-killed-cosmetic"
        return "survived-harness-test-killed"
    if not reached:
        return "survived-neither-line-unexecuted"
    return "survived-neither"


def audit(
    report: dict[str, Any],
    executed: set[int],
    case_count: int,
    spans: dict[int, tuple[int, int]],
) -> dict[str, Any]:
    rows = report["results"]
    labels: collections.Counter[str] = collections.Counter()
    per_operator: dict[str, dict[str, int]] = collections.defaultdict(
        lambda: {"mutants": 0, "harness": 0, "tests": 0, "neither": 0}
    )
    case_frequency: collections.Counter[str] = collections.Counter()
    differing_histogram: collections.Counter[int] = collections.Counter()
    annotated: list[dict[str, Any]] = []
    for row in rows:
        reached = _reached(row["line"], spans, executed)
        label = classify(row, reached, case_count)
        labels[label] += 1
        row = dict(row, classification=label, line_executed=reached)
        annotated.append(row)
        stats = per_operator[row["operator"]]
        stats["mutants"] += 1
        stats["harness"] += 1 if row.get("harness_killed") else 0
        stats["tests"] += 1 if row.get("tests_killed") else 0
        if not row.get("harness_killed") and not row.get("tests_killed"):
            stats["neither"] += 1
        for case_id in row.get("differing_cases") or []:
            case_frequency[case_id] += 1
        differing_histogram[row.get("differing_count", 0)] += 1
    both = sum(1 for r in rows if r.get("harness_killed") and r.get("tests_killed"))
    harness_only = sum(1 for r in rows if r.get("harness_killed") and not r.get("tests_killed"))
    tests_only = sum(1 for r in rows if not r.get("harness_killed") and r.get("tests_killed"))
    neither = len(rows) - both - harness_only - tests_only
    return {
        "mutants": len(rows),
        "case_count": case_count,
        "executed_lines": len(executed),
        "contingency": {
            "both": both,
            "harness_only": harness_only,
            "tests_only": tests_only,
            "neither": neither,
        },
        "labels": dict(sorted(labels.items())),
        "per_operator": dict(sorted(per_operator.items())),
        "differing_histogram": {str(k): v for k, v in sorted(differing_histogram.items())},
        "case_frequency": case_frequency.most_common(15),
        "mutants_on_unexecuted_lines": sum(
            1 for r in rows if not _reached(r["line"], spans, executed)
        ),
        "rows": annotated,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", default="docs/results/mutation-pycodestyle-20261121.json")
    parser.add_argument("--lines", default=".harness/stage2-executed-lines.json")
    parser.add_argument("--cases", default="harness/cases/pycodestyle-cli.json")
    parser.add_argument(
        "--source",
        default=".harness/verify-trees/old/pycodestyle.py",
        help="the module the sites were enumerated from, for statement spans",
    )
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    report = json.loads((REPO / args.results).read_text(encoding="utf-8"))
    lines = json.loads((REPO / args.lines).read_text(encoding="utf-8"))
    executed = set(lines["executed"])
    cases = json.loads((REPO / args.cases).read_text(encoding="utf-8"))["cases"]
    spans = statement_spans((REPO / args.source).read_text(encoding="utf-8"))
    result = audit(report, executed, len(cases), spans)

    print(f"mutants {result['mutants']}  cases {result['case_count']}  "
          f"executed lines {result['executed_lines']}")
    print(f"contingency {result['contingency']}")
    print(f"on unexecuted lines: {result['mutants_on_unexecuted_lines']}")
    for label, count in result["labels"].items():
        print(f"  {label:40} {count}")
    print("differing-case frequency (top 10):")
    for case_id, count in result["case_frequency"][:10]:
        print(f"  {count:3}  {case_id}")
    print("per operator (mutants / harness / tests / neither):")
    for name, stats in result["per_operator"].items():
        print(f"  {name:12} {stats['mutants']:3} / {stats['harness']:3} / "
              f"{stats['tests']:3} / {stats['neither']:3}")

    if args.out:
        (REPO / args.out).write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
        print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
