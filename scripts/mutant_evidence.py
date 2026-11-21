"""Show the evidence for one mutant: the edit, the cases that noticed, the diff.

A kill is a claim, and a claim about behaviour needs a witness. This prints, for a
given site:

* the exact line the mutation changed (old text and new text, and nothing else —
  if more than one line changes, the mutator has a bug);
* which cases differ, and in which field;
* a unified diff of the first differing value, so a reader can judge whether the
  difference is the behaviour or an artefact of the observation;
* what the upstream suite said.

    python scripts/mutant_evidence.py --sites 'int_plus@191:36' 'delete_stmt@396:8'
"""

from __future__ import annotations

import argparse
import difflib
import json
import shutil
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "src"))

import mutate as runner  # noqa: E402

from harness import determinism  # noqa: E402
from harness.cases import case_set_hash, load_cases  # noqa: E402
from harness.compare import compare_sets  # noqa: E402
from harness.mutate import enumerate_sites, mutate  # noqa: E402


def _diff_lines(left: Any, right: Any) -> list[str]:
    if isinstance(left, str) and isinstance(right, str):
        return list(
            difflib.unified_diff(
                left.splitlines(), right.splitlines(), "baseline", "mutant", lineterm="", n=1
            )
        )
    return [f"  baseline: {json.dumps(left)[:200]}", f"  mutant:   {json.dumps(right)[:200]}"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default=".harness/verify-trees/old")
    parser.add_argument("--module", default="pycodestyle")
    parser.add_argument("--cases", default="harness/cases/pycodestyle-cli.json")
    parser.add_argument("--tests", default="tests")
    parser.add_argument("--tests-subdir", default="tests")
    parser.add_argument("--sites", nargs="+", required=True)
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    target = (REPO / args.target).resolve()
    cases = load_cases(REPO / args.cases)
    work = REPO / ".harness" / "mutant-evidence"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    shim = determinism.write_shim(work / "shim")
    module_file = runner._module_file(target, args.module)
    source = module_file.read_text(encoding="utf-8")
    sites = {site.key: site for site in enumerate_sites(source)}

    baseline = runner._one_pass(
        target, cases=cases, shim=shim, workdir=work / "baseline", index=1,
        module=args.module,
    )
    base_by_case = baseline.by_case()
    rows: list[dict[str, Any]] = []
    for kind in args.sites:
        site = sites[kind]
        mutated = mutate(source, site)
        tree = runner._write_mutant(target, work / "mutant", site, source, args.module)
        ran = runner._one_pass(
            tree, cases=cases, shim=shim, workdir=work / f"run-{site.key.replace(':', '-')}",
            index=2, module=args.module,
        )
        verdicts = compare_sets(base_by_case, ran.by_case())
        differing = sorted(
            case_id for case_id, verdict in verdicts.items()
            if verdict.report_class == "different"
        )
        # The changed region, not a line-by-line zip: deleting one line shifts
        # every line after it, and a naive zip then calls a one-line deletion a
        # 2572-line edit.
        before_lines = source.splitlines()
        after_lines = mutated.splitlines()
        head = 0
        while head < min(len(before_lines), len(after_lines)) and (
            before_lines[head] == after_lines[head]
        ):
            head += 1
        tail = 0
        while tail < min(len(before_lines), len(after_lines)) - head and (
            before_lines[len(before_lines) - 1 - tail] == after_lines[len(after_lines) - 1 - tail]
        ):
            tail += 1
        changed = [
            (head + 1, " | ".join(before_lines[head : len(before_lines) - tail]),
             " | ".join(after_lines[head : len(after_lines) - tail]))
        ]
        removed = len(before_lines) - tail - head
        added = len(after_lines) - tail - head
        tests_dir = tree / args.tests_subdir if args.tests_subdir else (REPO / args.tests)
        killed, note, detail = runner._run_tests(tests_dir, tree, stop_early=False)

        print(f"\n=== {site.key}  ({len(changed)} line(s) changed)")
        for number, before, after in changed:
            print(f"  {number}-  {before.rstrip()}")
            print(f"  {number}+  {after.rstrip()}")
        print(f"  harness: {len(differing)} of {len(cases)} cases differ")
        for case_id in differing[:6]:
            left = json.loads(base_by_case[case_id].to_json()).get("value")
            right = json.loads(ran.by_case()[case_id].to_json()).get("value")
            if not isinstance(left, dict) or not isinstance(right, dict):  # pragma: no cover
                continue
            fields = [name for name in right if right.get(name) != left.get(name)]
            print(f"    {case_id}  fields={fields}")
            for name in fields[:1]:
                for line in _diff_lines(left.get(name), right.get(name))[:12]:
                    print("      " + line)
        print(f"  suite: {'killed' if killed else 'passed'} — {note}")
        for failure in (detail.get("failures") or [])[:5]:
            print(f"    failed: {failure}")
        rows.append(
            {
                "site": site.key,
                "changed_lines": [
                    {"line": number, "before": before, "after": after}
                    for number, before, after in changed
                ],
                "lines_removed": removed,
                "lines_added": added,
                "differing_cases": differing,
                "tests_killed": killed,
                "tests_note": note,
                "tests_failures": detail.get("failures") or [],
            }
        )
    if args.out:
        payload = {
            "target": args.module,
            "case_set_hash": case_set_hash(cases),
            "case_count": len(cases),
            "mutants": rows,
        }
        (REPO / args.out).write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
        print(f"\n-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
