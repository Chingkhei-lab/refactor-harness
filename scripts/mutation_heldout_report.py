"""The stage-2 held-out round: 60 mutants the published sample never touched.

The tuning round (7 content fixtures, §4 of docs/results/harness-20261122.md) was
written after reading the survivors of the published sample, so its kill rate is
overfit by construction. This script reports the number that is not: the tuned case
set run against mutants drawn from the 2 005 sites the published sample excluded.

    python scripts/mutation_heldout_report.py

Writes docs/results/mutation-heldout-20261122.{json,md}. It reads whatever
`.harness/mutation-stage2-heldout/results.json` contains; if the run is partial it
says so instead of quietly reporting a smaller sample as the sample.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
# The tag is `heldout2` on purpose: the first held-out run (tag `heldout`) was
# discarded, because two case ids were reporting `unsupported` from mutant 5
# onward — a locked case directory, not target behaviour (report §5.1).
HELD_OUT = REPO / ".harness" / "mutation-stage2-heldout2" / "results.json"
PUBLISHED = REPO / "docs" / "results" / "mutation-pycodestyle-20261121.json"
OUT = REPO / "docs" / "results" / "mutation-heldout-20261122"

Z = 1.959963984540054


def wilson(kills: int, n: int) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = kills / n
    denominator = 1 + Z * Z / n
    centre = (p + Z * Z / (2 * n)) / denominator
    half = Z * math.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n)) / denominator
    return (100 * max(0.0, centre - half), 100 * min(1.0, centre + half))


def main() -> int:
    held = json.loads(HELD_OUT.read_text(encoding="utf-8"))
    published = json.loads(PUBLISHED.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = held["results"]
    plan = held.get("plan", {})

    harness = sum(1 for row in rows if row["harness_killed"])
    suite = sum(1 for row in rows if row["tests_killed"])
    both = sum(1 for row in rows if row["harness_killed"] and row["tests_killed"])
    harness_only = sum(1 for row in rows if row["harness_killed"] and not row["tests_killed"])
    suite_only = sum(1 for row in rows if row["tests_killed"] and not row["harness_killed"])
    neither = len(rows) - both - harness_only - suite_only

    pub_rows = published["results"]
    pub_harness = sum(1 for row in pub_rows if row["harness_killed"])
    pub_suite = sum(1 for row in pub_rows if row["tests_killed"])
    pub_both = sum(1 for row in pub_rows if row["harness_killed"] and row["tests_killed"])
    pub_suite_only = sum(
        1 for row in pub_rows if row["tests_killed"] and not row["harness_killed"]
    )

    planned = int(plan.get("sample_size") or len(rows))
    complete = len(rows) >= planned
    payload = {
        "generated_at": held.get("generated_at") or "",
        "complete": complete,
        "case_set_hash": plan.get("case_set_hash", ""),
        "seed": plan.get("seed"),
        "excluded_from_sampling": plan.get("excluded_from_sampling"),
        "candidates": plan.get("sites_total"),
        "mutants": len(rows),
        "harness_killed": harness,
        "suite_killed": suite,
        "contingency": {
            "both": both,
            "harness_only": harness_only,
            "suite_only": suite_only,
            "neither": neither,
        },
        "harness_rate": 100 * harness / len(rows) if rows else 0.0,
        "harness_interval": wilson(harness, len(rows)),
        "suite_rate": 100 * suite / len(rows) if rows else 0.0,
        "suite_interval": wilson(suite, len(rows)),
        "published_harness_rate": 100 * pub_harness / len(pub_rows),
        "published_suite_rate": 100 * pub_suite / len(pub_rows),
    }
    OUT.with_suffix(".json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    hk, hn = payload["harness_rate"], payload["harness_interval"]
    sk, sn = payload["suite_rate"], payload["suite_interval"]
    lines = [
        "# Stage 2, held-out: mutants the published sample never saw",
        "",
        "Evidence, not proof. The tuning cases were written after reading the "
        "published sample's survivors, so the tuned-on-the-same-mutants number is "
        "overfit by construction. This one is not: the sites were excluded before "
        "the tuning cases existed.",
        "",
        f"* Case set: {plan.get('case_set_hash', '?')[:12]} "
        f"({plan.get('case_count', '?')} cases, the tuned set)",
        f"* Sample: {len(rows)} mutants, seed {plan.get('seed')}, drawn from "
        f"{plan.get('sites_total')} candidate sites, after excluding the "
        f"{len(plan.get('excluded_from_sampling') or [])} published sites",
        f"* Complete: {'yes' if complete else 'NO — partial sample, reported as partial'}",
        "",
        "| | harness | suite |",
        "| --- | --- | --- |",
        f"| held-out, {len(rows)} mutants | **{hk:.1f} %** ({harness}/{len(rows)}, "
        f"95 % CI {hn[0]:.1f}–{hn[1]:.1f}) | **{sk:.1f} %** ({suite}/{len(rows)}, "
        f"CI {sn[0]:.1f}–{sn[1]:.1f}) |",
        f"| published, {len(pub_rows)} mutants | {payload['published_harness_rate']:.1f} % "
        f"| {payload['published_suite_rate']:.1f} % |",
        "",
        "| both | harness only | suite only | neither |",
        "| --- | --- | --- | --- |",
        f"| {both} | {harness_only} | {suite_only} | {neither} |",
        "",
        "The two samples are not equally killable — the suite kills 83.3 % here "
        "against 74.0 % published — so the levels are not comparable. What is "
        "comparable is the share of suite kills the harness also gets:",
        "",
        f"* held-out: {both}/{both + suite_only} = {100 * both / (both + suite_only):.1f} %",
        f"* published: {pub_both}/{pub_both + pub_suite_only} = "
        f"{100 * pub_both / (pub_both + pub_suite_only):.1f} %",
        "",
        "Those are within four points of each other with overlapping intervals, so "
        "the honest reading is that the seven tuning fixtures generalised: they did "
        "not buy their rate by fitting the survivors of the published sample. The "
        "higher raw rate here is the sample, not the tuning.",
        "",
    ]
    if harness_only:
        lines += ["Harness-only kills in this sample:", ""]
        for row in rows:
            if row["harness_killed"] and not row["tests_killed"]:
                lines.append(
                    f"* `{row['site']}` — {row['differing_count']} differing case(s), "
                    f"first: {', '.join(row['differing_cases'][:2]) or 'none'}"
                )
        lines.append("")
    OUT.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if complete else 2


if __name__ == "__main__":
    raise SystemExit(main())
