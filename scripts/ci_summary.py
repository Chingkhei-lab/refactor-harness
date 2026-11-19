"""Turn pytest JUnit XML into a `$GITHUB_STEP_SUMMARY` block.

The reason this exists: the checkpoint-9 review asked for per-job test counts
and the only honest answer at the time was "I ran the same commands locally",
because the Actions job log needs admin rights this token does not have. A run
page should report its own counts, so every job now writes them where anyone
can read them.

Usage (in a workflow step):

    python scripts/ci_summary.py --job ci junit-ci.xml >> "$GITHUB_STEP_SUMMARY"

Counts are derived from the XML, never from the console tail, so a truncated
log cannot change the number.
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path


def _counts(case: ET.Element) -> str:
    if case.find("failure") is not None or case.find("error") is not None:
        return "failed"
    if case.find("skipped") is not None:
        return "skipped"
    return "passed"


def summarise(job: str, paths: list[Path]) -> str:
    rows: list[tuple[str, Counter[str]]] = []
    reasons: Counter[tuple[str, str]] = Counter()
    totals: Counter[str] = Counter()

    for path in paths:
        tree = ET.parse(path)  # noqa: S314 - our own JUnit XML
        for suite in tree.iter("testsuite"):
            name = suite.get("name") or path.name
            bucket: Counter[str] = Counter()
            for case in suite.iter("testcase"):
                bucket[_counts(case)] += 1
                skipped = case.find("skipped")
                if skipped is not None:
                    reasons[(name, skipped.get("message", "no reason recorded"))] += 1
            if bucket:
                rows.append((name, bucket))
                totals.update(bucket)

    collected = sum(totals.values())
    lines = [
        f"### `{job}` - {collected} collected, {totals['passed']} passed, "
        f"{totals['skipped']} skipped, {totals['failed']} failed",
        "",
        "| test file | collected | passed | skipped | failed |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name, bucket in sorted(rows):
        lines.append(
            f"| `{name}` | {sum(bucket.values())} | {bucket['passed']} | "
            f"{bucket['skipped']} | {bucket['failed']} |"
        )
    if reasons:
        lines += ["", "**Skip reasons**", ""]
        for (name, reason), count in sorted(reasons.items()):
            lines.append(f"- {count} x `{name}` - {reason}")
    else:
        lines += ["", "No skips."]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True)
    parser.add_argument("xml", nargs="+", type=Path)
    args = parser.parse_args()
    missing = [str(p) for p in args.xml if not p.exists()]
    if missing:
        print(f"::error::missing junit report(s): {', '.join(missing)}")
        return 1
    sys.stdout.write(summarise(args.job, args.xml))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
