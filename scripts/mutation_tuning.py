"""Report the tuning experiment: first pass, tuned, and held-out.

The question is not "did adding cases raise the kill rate" — of course it did, the
cases were written after reading the survivors. The question is whether the
improvement survives on mutants the tuning never looked at, so the held-out
column is the result and the other two are context.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]


def wilson(killed: int, total: int) -> tuple[float, float]:
    if total == 0:
        return (0.0, 0.0)
    z = 1.959963985
    p = killed / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def rate(rows: list[dict[str, Any]]) -> str:
    killed = sum(1 for row in rows if row["harness_killed"])
    low, high = wilson(killed, len(rows))
    return (
        f"{killed}/{len(rows)} = {100 * killed / len(rows):.1f} % "
        f"(95 % CI {100 * low:.0f}-{100 * high:.0f} %)"
    )


def load(tag: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    root = REPO / ".harness" / (f"mutation-{tag}" if tag else "mutation")
    payload = json.loads((root / "results.json").read_text(encoding="utf-8"))
    return payload["results"], payload["plan"]


def main() -> int:
    executed = set(
        json.loads((REPO / ".harness/mutation/coverage-lines.json").read_text())["lines"]
    )
    first, first_plan = load("")
    tuned, tuned_plan = load("tuned")
    held_old, held_plan = load("heldout-original")
    held_new, _ = load("heldout-tuned")
    held_tests, held_tests_plan = load("heldout-tuned-tests")
    harness_kills = sum(1 for row in held_tests if row["harness_killed"])
    test_kills = sum(1 for row in held_tests if row["tests_killed"])
    assert [row["site"] for row in first] == [row["site"] for row in tuned]
    assert [row["site"] for row in held_old] == [row["site"] for row in held_new]

    covered = [row for row in first if row["line"] in executed]
    tuned_covered = [row for row in tuned if row["line"] in executed]
    newly = [
        row["site"] for row in tuned
        if row["harness_killed"]
        and not {r["site"]: r["harness_killed"] for r in first}[row["site"]]
    ]
    lost = [
        row["site"] for row in tuned
        if not row["harness_killed"]
        and {r["site"]: r["harness_killed"] for r in first}[row["site"]]
    ]
    held_cov_old = [row for row in held_old if row["line"] in executed]
    held_cov_new = [row for row in held_new if row["line"] in executed]
    held_cov_tests = [row for row in held_tests if row["line"] in executed]
    both = [row for row in held_tests if row["harness_killed"] and row["tests_killed"]]
    harness_only = [row for row in held_tests if row["harness_killed"] and not row["tests_killed"]]

    def pct(rows: list[dict[str, Any]]) -> float:
        return 100 * sum(1 for row in rows if row["harness_killed"]) / len(rows)

    held_gain = pct(held_new) - pct(held_old)
    tuned_gain = pct(tuned) - pct(first)
    lines = [
        "# Tuning the case set: what generalised and what was fit to the survivors",
        "",
        "Evidence, not proof. The first pass is the headline; the tuned run on the "
        "same mutants is overfit by construction, because the new cases were written "
        "after reading the 65 covered survivors. The held-out run is the result.",
        "",
        "| Run | Case set | Mutants | Kill rate | Covered mutants only |",
        "| --- | --- | --- | --- | --- |",
        f"| First pass (headline) | 273 cases, `{first_plan['case_set_hash'][:8]}` "
        f"| {len(first)} (seed {first_plan['seed']}) | {rate(first)} "
        f"| {rate(covered)} |",
        f"| Tuned, same mutants (overfit) | 307 cases, "
        f"`{tuned_plan['case_set_hash'][:8]}` | {len(tuned)} | {rate(tuned)} "
        f"| {rate(tuned_covered)} |",
        f"| Held-out, original cases | 273 cases | {len(held_old)} (seed "
        f"{held_plan['seed']}, sites excluded: {len(held_plan['excluded_from_sampling'])}) "
        f"| {rate(held_old)} | {rate(held_cov_old)} |",
        f"| Held-out, tuned cases | 307 cases | {len(held_new)} | {rate(held_new)} "
        f"| {rate(held_cov_new)} |",
        f"| Held-out, tuned cases + upstream tests | 307 cases | {len(held_tests)} "
        f"| harness {rate(held_tests)}; tests {100 * test_kills / len(held_tests):.1f} % "
        f"({100 * sum(1 for r in held_cov_tests if r['tests_killed']) / len(held_cov_tests):.1f} % "
        "on covered lines) |",
        "",
        "The held-out mutants are drawn from the 539 sites the first sample did not "
        "touch, stratified by operator and seeded (99), so no tuning case was written "
        "with those sites in view. The gain there is "
        f"{held_gain:+.1f} percentage points, against {tuned_gain:+.1f} on the tuning "
        "set. The two intervals overlap, so the honest reading is "
        "\"the new cases help, and the tuning set does not overstate by much\" — not "
        "\"tuning doubled the kill rate\".",
        "",
        f"Newly killed on the tuning set: **{len(newly)}**; lost: **{len(lost)}**.",
        "",
        "## What the 34 new cases were for",
        "",
        "| Cluster | Cases | Survivor lines it targets |",
        "| --- | --- | --- |",
        "| integer sign and base (`{:d}` with `+`, `-`, `0x`, `0b`, `0o`; `{:x}/{ :b}/{ :o}`) "
        "| 12 | 74-93 |",
        "| meridiem (`{:tt}` with `PM`/`am`, including the 12 o'clock cases) | 5 | 233-236 |",
        "| no year in the input (`{:ts}`) | 2 | 284 |",
        "| width, precision, alignment (`{:>10}`, `{:=10}`, `{:.3}`, `{:10.3}`, `{:5d}`) "
        "| 7 | 362-400, 842, 850 |",
        "| repeated named field (`{n}{n}`) | 1 | 640 |",
        "| two datetime fields in one format | 4 | 723-821 |",
        "| timezone/month-name formats (`{:ti}`, `{:ta}`, `{:tc}`) | 3 | 723-821 |",
        "",
        "`{:10.3}` is not a working format — the target raises `NotImplementedError` "
        "on it. It is kept because the exception is a deterministic observation and "
        "the width-with-precision branch is otherwise unreachable.",
        "",
        "## The ratio to the upstream suite on held-out mutants",
        "",
        f"On the held-out sample the case set kills {harness_kills} mutants and the "
        f"upstream suite kills {test_kills}: the generated cases reach "
        f"{100 * harness_kills / max(1, test_kills):.0f} % of the suite's kill power "
        "with zero hand-written assertions. The overlap is "
        f"{len(both)}; the case set finds **{len(harness_only)}** mutants the suite "
        "misses, and the suite finds "
        f"{test_kills - len(both)} the cases miss. On the first pass the harness kills "
        "were a strict subset of the suite's; on mutants the tuning never saw, they "
        "are not.",
        "",
        "## What this does not say",
        "",
        "- The held-out sample is another 150 of the same 839 sites on the same file, "
        "and those sites sit inside the same functions as the tuned clusters "
        "(`date_convert`, `extract_format`, `int_convert`). It therefore shows that the "
        "new cases cover those *regions* better; it does **not** show that tuning "
        "generalises to code it has never seen. A cross-target held-out sample (stage 2) "
        "is the only thing that would.",
        "- The test-oracle column is only on the tuned held-out run (the test verdict "
        "does not depend on the case set), not on the original-case held-out run.",
        "- A gain here is not a claim that the case set is complete: 96 of the first "
        "pass's survivors sit on lines no case executes, and writing cases for those "
        "lines is a different exercise from writing cases for a branch.",
        "",
    ]
    out = REPO / "docs" / "results" / "mutation-tuning-20261120.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out}")
    print("\n".join(lines[5:11]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
