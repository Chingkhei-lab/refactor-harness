"""Publish JUnit counts as a commit status, so they are readable without a token.

The job logs need admin rights and the JUnit artifacts need a token; a commit
status is readable with a plain GET on a public repository. Each job posts one
status whose description is `collected / passed / skipped (+ skip reasons)`, which
is enough to answer "did the CPU-budget test run or skip on CI?" from outside.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

MAX_DESCRIPTION = 140


def counts(path: Path) -> tuple[Counter[str], int, list[str]]:
    root = ET.parse(path).getroot()
    suite = root if root.tag == "testsuite" else list(root)[0]
    total = int(suite.get("tests") or 0)
    failed = int(suite.get("failures") or 0) + int(suite.get("errors") or 0)
    skipped = int(suite.get("skipped") or 0)
    reasons: Counter[str] = Counter()
    for case in suite.iter("testcase"):
        skip = case.find("skipped")
        if skip is not None:
            reasons[(skip.get("message") or "no reason").strip()[:60]] += 1
    return (
        Counter({"collected": total, "passed": total - failed - skipped,
                 "failed": failed, "skipped": skipped}),
        failed,
        [f"{count}x {reason}" for reason, count in reasons.most_common(3)],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True, help="status context, e.g. junit/ci")
    parser.add_argument("--file", action="append", required=True)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--sha", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    totals: Counter[str] = Counter()
    failed = 0
    reasons: list[str] = []
    for name in args.file:
        found, failures, why = counts(Path(name))
        totals.update(found)
        failed += failures
        reasons += why
    description = (
        f"collected {totals['collected']} / passed {totals['passed']} / "
        f"skipped {totals['skipped']} / failed {totals['failed']}"
    )
    if reasons:
        description += " | " + "; ".join(reasons)
    description = description[:MAX_DESCRIPTION]
    state = "success" if failed == 0 else "failure"
    print(f"{args.context}: {state} - {description}")
    if args.dry_run or not (args.repo and args.sha and args.token):
        return 0 if failed == 0 else 1
    request = urllib.request.Request(
        f"https://api.github.com/repos/{args.repo}/statuses/{args.sha}",
        data=json.dumps({"state": state, "context": args.context,
                         "description": description}).encode(),
        headers={
            "Authorization": f"Bearer {args.token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        print("posted:", response.status)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
