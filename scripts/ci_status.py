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
from typing import Any

MAX_DESCRIPTION = 140


def failing(path: Path) -> list[str]:
    """Names of failing test cases, so a status carries a diagnosis, not a number."""
    root = ET.parse(path).getroot()
    suite = root if root.tag == "testsuite" else list(root)[0]
    names = []
    for case in suite.iter("testcase"):
        if case.find("failure") is not None or case.find("error") is not None:
            names.append(f"{case.get('classname', '')}::{case.get('name', '')}")
    return names


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
    parser.add_argument(
        "--issue-title",
        default="",
        help="also comment the counts on a tracking issue with this title "
        "(created if missing) — readable without a token, unlike job logs",
    )
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
    failing_names: list[str] = []
    for name in args.file:
        failing_names += failing(Path(name))
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

    if args.issue_title:
        _comment_on_issue(args, description, failing_names)
    return 0  # the job's own exit code already reports the failure


def _api(
    args: argparse.Namespace, path: str, payload: dict[str, Any] | None = None
) -> Any:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{args.repo}{path}",
        data=None if payload is None else json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {args.token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
        method="GET" if payload is None else "POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode())


def _comment_on_issue(
    args: argparse.Namespace, description: str, failing_names: list[str]
) -> None:
    """A comment on a tracking issue is the one channel readable without a token."""
    if not (args.repo and args.token):
        return
    issue = None
    for candidate in _api(args, "/issues?state=all&per_page=100"):
        if candidate.get("title") == args.issue_title:
            issue = candidate
            break
    if issue is None:
        issue = _api(args, "/issues", {"title": args.issue_title, "body": "CI self-reports."})
    body = f"**{args.context}** on `{args.sha[:7]}`: {description}"
    if failing_names:
        body += chr(10) + chr(10) + "Failing:" + chr(10) + chr(10)
        body += chr(10).join(f"- `{name}`" for name in failing_names[:8])
    _api(args, f"/issues/{issue['number']}/comments", {"body": body})
    print(f"commented on issue #{issue['number']}")


if __name__ == "__main__":
    raise SystemExit(main())
