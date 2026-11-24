"""The frozen surface of an agent run: one hash over everything the evaluation holds fixed.

The evaluation compares models and tasks, so the things that shape a run must not move
while it is running: the system prompt, the tool schemas the model sees, the wrap-up
protocol, the budget defaults and caps, and the guardrail code. This script prints them
canonically and hashes the manifest; the hash goes into ADR 006 and the JSON beside this
script is the record. A change to any of them is a new freeze and a new evaluation, never
a quiet edit under a running one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agent.live import TOOL_SCHEMAS  # noqa: E402
from agent.loop import TERMINAL_TOOLS, WRAPUP_TOOLS, WRAPUP_WINDOW  # noqa: E402
from agent.prompts import PROMPT_VERSION, SYSTEM_PROMPT, system_prompt_sha256  # noqa: E402
from agent.tasks import TASK_DIR  # noqa: E402
from agent.types import Budget, canonical_json, sha256_text  # noqa: E402

GUARDRAILS = REPO_ROOT / "src" / "agent" / "guardrails.py"
LOOP = REPO_ROOT / "src" / "agent" / "loop.py"
CASES = REPO_ROOT / "harness" / "cases"
RULES = REPO_ROOT / "harness" / "rules"
GOLDEN = REPO_ROOT / "harness" / "golden"

#: D25 caps for the first evaluation pass. Per-run budgets are `Budget` defaults.
EVALUATION_CAPS = {
    "runs": 24,
    "max_total_tokens": 1_500_000,
    "max_wall_seconds": 8 * 60 * 60,
}


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_sha256(paths: list[Path]) -> str:
    """A hash over relative names and bytes, so a rename is a change too."""
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.name.encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def manifest() -> dict[str, Any]:
    task_files = sorted(TASK_DIR.glob("*.json"))
    case_files = sorted(CASES.glob("*.json"))
    rule_files = sorted(RULES.glob("*.json"))
    golden_files = sorted(GOLDEN.glob("*.json"))
    return {
        "schema": "agent-freeze/1",
        "prompt": {
            "version": PROMPT_VERSION,
            "sha256": system_prompt_sha256(),
            "chars": len(SYSTEM_PROMPT),
        },
        "tools": {
            "sha256": sha256_text(canonical_json(list(TOOL_SCHEMAS))),
            "names": [str(tool["function"]["name"]) for tool in TOOL_SCHEMAS],
            "count": len(TOOL_SCHEMAS),
        },
        "wrapup": {
            "window": WRAPUP_WINDOW,
            "tools": sorted(WRAPUP_TOOLS),
            "terminal_tools": sorted(TERMINAL_TOOLS),
            "reserved_calls": 1,
            "tool_choice": "required",
            # The behavior lives in the loop, not only in the constants above.
            "loop_sha256": _file_sha256(LOOP),
        },
        "budgets": {
            "run_defaults": Budget().to_json(),
            "evaluation_caps": EVALUATION_CAPS,
        },
        "guardrails": {
            "module": "src/agent/guardrails.py",
            "sha256": _file_sha256(GUARDRAILS),
        },
        "task_set": {
            "sha256": _tree_sha256(task_files),
            "count": len(task_files),
            "files": [path.name for path in task_files],
            # The success ladder is not data: a change to it changes what a run means.
            "classifier_sha256": _file_sha256(REPO_ROOT / "src" / "agent" / "tasks.py"),
        },
        "harness_inputs": {
            "cases_sha256": _tree_sha256(case_files),
            "rules_sha256": _tree_sha256(rule_files),
            "golden_sha256": _tree_sha256(golden_files),
        },
    }


def freeze() -> dict[str, Any]:
    body = manifest()
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return {
        "schema": "agent-freeze/1",
        "freeze_sha256": sha256_text(canonical_json(body)),
        "git_commit": commit,
        "manifest": body,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="write the record to this path")
    parser.add_argument("--check", type=Path, help="compare a recorded file with this tree")
    args = parser.parse_args(argv)

    record = freeze()
    if args.check is not None:
        recorded = json.loads(args.check.read_text(encoding="utf-8"))
        if recorded.get("freeze_sha256") != record["freeze_sha256"]:
            old = recorded.get("freeze_sha256")
            print(
                f"freeze changed: {args.check} has {old}, this tree is {record['freeze_sha256']}",
                file=sys.stderr,
            )
            return 1
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    print(json.dumps(record, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
