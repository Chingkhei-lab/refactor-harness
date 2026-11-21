"""No-op canaries: a mutant that changes nothing must be killed by nothing.

A mutation score is only meaningful if the evaluator can say "survived". The
stage-1 canary test covers the `parse` fixture layout; this covers a real target
through the real pipeline, for both oracles, and it is the check that decides
whether a 100 % score means "the cases are sensitive" or "the runner is broken".

Three canaries, all of them behaviour-preserving by construction:

* ``comment``     — a comment line appended at end of file (line numbers of every
                    existing statement are unchanged, so a case that prints a
                    traceback cannot notice it);
* ``whitespace``  — extra spaces around an assignment operator on a line that
                    does not execute? No: on a line that *does* execute, which is
                    the stricter case;
* ``docstring``   — one word changed inside a module docstring (the module's own
                    ``__doc__`` is public API, so this one is expected to be
                    observed only by an api snapshot, never by a CLI run).

Each canary is applied to a fresh copy of the tree, run through the same
``_one_pass`` the mutation runner uses, compared against the unmutated baseline
with ``compare_sets``, and then the upstream suite is run against it with
``_run_tests`` — the same two oracles, the same code paths, no special casing.

    python scripts/mutation_canary.py --target .harness/verify-trees/old \
        --tests .harness/oracle/pycodestyle/tests --module pycodestyle
"""

from __future__ import annotations

import argparse
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


def _apply(source: str, kind: str) -> tuple[str, str]:
    """Return (mutated source, one-line description of the edit)."""
    if kind == "comment":
        return source + "\n# canary: this line changes no behaviour\n", "append a comment"
    if kind == "whitespace":
        # A line that certainly executes: the module's own `import` banner is
        # fragile, so use the first plain assignment at module level.
        for number, line in enumerate(source.splitlines(keepends=True)):
            stripped = line.lstrip()
            if " = " in line and not stripped.startswith(("#", '"', "'")):
                changed = line.replace(" = ", "  =  ", 1)
                lines = source.splitlines(keepends=True)
                lines[number] = changed
                return "".join(lines), f"spaces around `=` on line {number + 1}"
        raise SystemExit("no assignment found for the whitespace canary")
    if kind == "reformat":
        # The strongest no-op: `ast.unparse` is behaviour-preserving and rewrites
        # every line number and every echoed source line. It is also exactly what
        # this mutator used to do to every mutant, which is how a 100 % kill rate
        # appeared on a target where the harness and the suite disagree about
        # everything else. Nothing may notice it.
        import ast

        return ast.unparse(ast.parse(source)) + "\n", "re-print the module (no semantic change)"
    if kind == "docstring":
        # Inside the *module* docstring, found through the AST: the first line
        # containing `"""` may be the closing line of a multi-line docstring, and
        # editing that produces a file that does not import — which reads as "the
        # suite killed it" for a canary that changed one word.
        import ast
        import re

        tree = ast.parse(source)
        first = tree.body[0]
        assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
        body = first.value
        assert isinstance(body.value, str)
        lines = source.splitlines(keepends=True)
        for index in range(body.lineno - 1, body.end_lineno):
            prefix = len('"""') if index == body.lineno - 1 else 0
            match = re.search(r"[A-Za-z]{4,}", lines[index][prefix:])
            if match:
                at = prefix + match.end()
                lines[index] = lines[index][:at] + "X" + lines[index][at:]
                return "".join(lines), f"one word in the module docstring (line {index + 1})"
        raise SystemExit("no word found in the module docstring")
    raise SystemExit(f"unknown canary {kind}")


# A no-op must be noticed by nothing. A docstring is not a no-op for the api
# oracle: `__doc__` is part of the module's public surface, and the snapshot is
# supposed to see it. Those two expectations are asserted separately rather than
# averaged into "ok".
EXPECTED_OBSERVERS: dict[str, set[str]] = {
    "docstring": {"api-module", "api-parser"},
}


def _debug_payload(observations: dict[str, Any], case_id: str) -> str:
    return json.dumps(json.loads(observations[case_id].to_json()))[:400]


def _field_diffs(
    before: dict[str, Any], after: dict[str, Any], case_ids: list[str]
) -> dict[str, list[str]]:
    """Which fields of the observation differ, per differing case."""
    found: dict[str, list[str]] = {}
    for case_id in case_ids:
        left = json.loads(before[case_id].to_json())
        right = json.loads(after[case_id].to_json())
        left = left.get("value") if isinstance(left, dict) else None
        right = right.get("value") if isinstance(right, dict) else None
        if not isinstance(left, dict) or not isinstance(right, dict):
            found[case_id] = [
                "<not a value observation>",
                "before=" + json.dumps(left)[:120],
                "after=" + json.dumps(right)[:120],
            ]
            continue
        fields = [name for name in sorted(right) if right.get(name) != left.get(name)]
        found[case_id] = fields
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default=".harness/verify-trees/old")
    parser.add_argument("--tests", default=".harness/oracle/pycodestyle/tests")
    parser.add_argument("--tests-subdir", default="")
    parser.add_argument("--module", default="pycodestyle")
    parser.add_argument("--cases", default="harness/cases/pycodestyle-cli.json")
    parser.add_argument("--out", default="docs/results/mutation-canary-20261121.json")
    parser.add_argument("--only", default="", help="comma-separated canaries")
    args = parser.parse_args(argv)

    target = (REPO / args.target).resolve()
    cases = load_cases(REPO / args.cases)
    work = REPO / ".harness" / "mutation-canary"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    shim = determinism.write_shim(work / "shim")
    module_file = runner._module_file(target, args.module)
    source = module_file.read_text(encoding="utf-8")

    baseline = runner._one_pass(
        target, cases=cases, shim=shim, workdir=work / "baseline", index=1,
        module=args.module,
    )
    base_by_case = baseline.by_case()
    print(f"baseline: {len(cases)} cases, hash {case_set_hash(cases)[:12]}", flush=True)

    kinds = [k for k in (args.only.split(",") if args.only else
                         ["comment", "whitespace", "reformat", "docstring"]) if k]
    rows: list[dict[str, Any]] = []
    for index, kind in enumerate(kinds, start=2):
        mutated, description = _apply(source, kind)
        tree = work / f"canary-{kind}"
        shutil.copytree(target, tree)
        (tree / module_file.relative_to(target)).write_text(mutated, encoding="utf-8")
        ran = runner._one_pass(
            tree, cases=cases, shim=shim, workdir=work / f"run-{kind}", index=index,
            module=args.module,
        )
        verdicts = compare_sets(base_by_case, ran.by_case())
        differing = sorted(
            case_id for case_id, verdict in verdicts.items()
            if verdict.report_class == "different"
        )
        tests_killed = None
        tests_note = ""
        tests_dir = tree / (args.tests_subdir or "tests")
        tests_detail: dict[str, Any] = {}
        if args.tests and tests_dir.is_dir():
            tests_killed, tests_note, tests_detail = runner._run_tests(
                tests_dir, tree, stop_early=False
            )
        differing_fields = _field_diffs(base_by_case, ran.by_case(), differing)
        cosmetic_only = bool(tests_killed) and all(
            "test_own_dog_food" in failure.replace(chr(92), "/")
            for failure in (tests_detail.get("failures") or [])
        )
        expected = EXPECTED_OBSERVERS.get(kind, set())
        row = {
            "canary": kind,
            "expected_observers": sorted(expected),
            "observed_exactly_as_expected": set(differing) == expected,
            "tests_cosmetic_only": cosmetic_only,
            "edit": description,
            "differing_cases": differing,
            "differing_count": len(differing),
            "harness_killed": bool(differing),
            "tests_killed": tests_killed,
            "tests_note": tests_note,
            "tests_detail": tests_detail,
            "differing_fields": differing_fields,
        }
        rows.append(row)
        print(
            f"{kind:11} {description:34} differing={len(differing):2} "
            f"tests={'KILLED' if tests_killed else 'passed'} {tests_note[:40]}",
            flush=True,
        )
        for case_id in differing[:5]:
            print(f"    differs: {case_id} {differing_fields.get(case_id)}", flush=True)
        for failure in tests_detail.get("failures", [])[:3]:
            print(f"    test failed: {failure}", flush=True)

    report = {
        "target": args.module,
        "case_set_hash": case_set_hash(cases),
        "case_count": len(cases),
        "canaries": rows,
        "ok": all(
            row["observed_exactly_as_expected"]
            and not (row["tests_killed"] and not row["tests_cosmetic_only"])
            for row in rows
        ),
    }
    (REPO / args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"ok={report['ok']}  -> {args.out}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
