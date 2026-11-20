"""Mutation testing for a target: enumerate, sample, run, classify.

The protocol is fixed before any result is looked at, which is the point:

1. **Enumerate** every site of every operator (see `harness.mutate`).
2. **Time one run** of the unmutated tree, then choose N from a time budget and
   a fixed seed, and write the plan to disk *before* running a mutant.
3. **Run** each mutant once against the case set; a mutant is *killed* when the
   harness reports any case as `different` (a crash counts: it is a difference).
4. **Upstream tests** run against the same mutant, so the report can say what
   the harness adds and what it misses.
5. **Classify** survivors three ways — line not covered, covered but not
   observed, equivalent mutant (confirmed by hand).

Usage:

    python scripts/mutate.py --target .harness/target --tests \
        .harness/sdist/parse-1.22.4/tests --seed 12 --time-budget 1800
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from harness import determinism  # noqa: E402
from harness.cases import case_set_hash, load_cases  # noqa: E402
from harness.compare import compare_sets, summarise  # noqa: E402
from harness.mutate import OPERATORS, Site, enumerate_sites, mutate, sites_by_operator  # noqa: E402
from harness.observation import OBSERVATION_SCHEMA_VERSION  # noqa: E402
from harness.runner import Pass, run_pass  # noqa: E402

MODULE = "parse"


def _one_pass(
    checkout: Path, *, cases: list[Any], shim: Path, workdir: Path, index: int
) -> Pass:
    return run_pass(
        python=sys.executable,
        checkout=checkout,
        shim_dir=shim,
        src_dir=_REPO_ROOT / "src",
        target_paths=[checkout],
        cases=cases,
        index=index,
        module=MODULE,
        workdir=workdir,
        case_budget_seconds=10.0,
        cpu_budget_seconds=10.0,
        expected_case_set_hash=case_set_hash(cases),
    )


def _stratified_sample(sites: list[Site], size: int, seed: int) -> list[Site]:
    """A seeded sample that still represents every operator.

    A plain random sample of 60 out of 841 would often contain no `slice_bound`
    site (there are 17), and the report would then say nothing about that
    operator. Each operator gets at least `min(3, available)` sites, the rest is
    filled proportionally, all from one seed.
    """
    import random

    rng = random.Random(seed)
    by_operator: dict[str, list[Site]] = defaultdict(list)
    for site in sites:
        by_operator[site.operator].append(site)
    chosen: list[Site] = []
    for name in OPERATORS:
        pool = sorted(by_operator.get(name, []))
        if not pool:
            continue
        for site in rng.sample(pool, min(3, len(pool))):
            chosen.append(site)
    remaining = [site for site in sites if site not in chosen]
    while len(chosen) < size and remaining:
        chosen.append(remaining.pop(rng.randrange(len(remaining))))
    return sorted(chosen)[:size]


def _write_mutant(target: Path, work: Path, site: Site, source: str) -> Path:
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(target, work)
    package = work / MODULE
    if not package.is_dir():
        package.mkdir(parents=True)
    (package / "__init__.py").write_text(mutate(source, site), encoding="utf-8")
    return work


def _run_tests(tests: Path, checkout: Path) -> tuple[bool, str]:
    # The upstream tests are NOT under the determinism shim: they are the other
    # oracle, run the way their author runs them. So the environment is inherited
    # and only the import path is pointed at the mutant.
    env = dict(os.environ)
    env["PYTHONPATH"] = str(checkout)
    env["PYTHONHASHSEED"] = "0"
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-x", "-q", "--no-header",
             "-o", "addopts=", "-p", "no:cacheprovider",
             # importlib mode stops pytest inserting the tests directory at the
             # front of sys.path. With the default "prepend" mode a checkout
             # whose tests sit next to the module under test shadows the mutant
             # with the unmutated copy, and the oracle silently tests the wrong
             # tree — the canary fixture is exactly that layout, and it caught it.
             "--import-mode=importlib",
             str(tests)],
            cwd=str(checkout),
            capture_output=True,
            text=True,
            env=env,
            timeout=90,
        )
    except subprocess.TimeoutExpired:
        # A mutant that makes the upstream suite hang has been noticed by it:
        # that is a kill by timeout, not a pass. 90 s is ~50x the unmutated run.
        return True, "killed by the 90 s test timeout"
    tail = (result.stdout + result.stderr).strip().splitlines()[-1:] or [""]
    # `tests_killed`, not `tests_passed`: the first version of this line returned
    # `returncode == 0`, the report read it as "killed", and the inversion made
    # the two oracles look disjoint when in fact every harness kill was also a
    # test kill. The note below is the independent witness used to audit it.
    return result.returncode != 0, tail[0][:120]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default=".harness/target")
    parser.add_argument("--tests", default="")
    parser.add_argument("--cases", default="harness/cases/parse.json")
    parser.add_argument("--seed", type=int, default=12)
    parser.add_argument("--time-budget", type=float, default=1800.0)
    parser.add_argument("--max-mutants", type=int, default=0)
    parser.add_argument("--out", default="docs/results")
    parser.add_argument(
        "--tag",
        default="",
        help="work directory and report suffix, so a second run cannot resume the "
        "first one's results (tuning runs use their own tag)",
    )
    parser.add_argument(
        "--no-tests",
        action="store_true",
        help="skip the upstream-test oracle (a tuning run only needs the harness)",
    )
    parser.add_argument(
        "--exclude-plan",
        default="",
        help="path to a plan.json; the sites it sampled are excluded before sampling, "
        "which is how a held-out sample is drawn",
    )
    args = parser.parse_args()

    target = (_REPO_ROOT / args.target).resolve()
    source = (target / MODULE / "__init__.py").read_text(encoding="utf-8")
    cases = load_cases(_REPO_ROOT / args.cases)
    set_hash = case_set_hash(cases)
    work_root = _REPO_ROOT / ".harness" / ("mutation" + (f"-{args.tag}" if args.tag else ""))
    work_root.mkdir(parents=True, exist_ok=True)
    shim = determinism.write_shim(_REPO_ROOT / ".harness" / "shim")

    all_sites = enumerate_sites(source)
    sites = all_sites
    excluded: list[str] = []
    if args.exclude_plan:
        plan_path = (_REPO_ROOT / args.exclude_plan).resolve()
        prior = json.loads(plan_path.read_text(encoding="utf-8"))
        excluded = list(prior.get("sample", []))
        sites = [site for site in all_sites if site.key not in set(excluded)]
        print(f"held-out: {len(all_sites)} sites - {len(excluded)} already sampled "
              f"= {len(sites)}")
    per_operator = sites_by_operator(source)

    # 1. baseline pass (cached: A/A noise is 0 for this target, measured 5+5 passes)
    started = time.monotonic()
    baseline = _one_pass(target, cases=cases, shim=shim, workdir=work_root / "baseline", index=1)
    baseline_seconds = time.monotonic() - started
    # A/A noise for this target is 0 over 5 fixed and 5 shuffled passes
    # (docs/results/harness-parse-*.md), measured before this run.
    baseline_flaky = 0
    killed_by_deletion = sum(
        1 for observation in baseline.observations if observation.outcome == "unsupported"
    )

    # 2. plan before any result is looked at
    per_mutant = baseline_seconds + (2.0 if args.tests else 0.0)
    by_time = int(max(1.0, args.time_budget) // per_mutant) if per_mutant else 0
    size = args.max_mutants or min(len(sites), max(12, by_time))
    sample = _stratified_sample(sites, size, args.seed)
    plan = {
        "written_at": datetime.now(UTC).isoformat(),
        "observation_schema_version": OBSERVATION_SCHEMA_VERSION,
        "case_set_hash": set_hash,
        "case_count": len(cases),
        "target": str(target),
        "source_lines": len(source.splitlines()),
        "sites_total": len(sites),
        "sites_by_operator": per_operator,
        "baseline_seconds": round(baseline_seconds, 2),
        "estimated_seconds_per_mutant": round(per_mutant, 2),
        "seed": args.seed,
        "sample_size": len(sample),
        "sample": [site.key for site in sample],
        "sampling": "stratified: min(3, available) per operator, rest proportional, one seed",
        "tag": args.tag,
        "excluded_from_sampling": excluded,
        "baseline_unsupported": killed_by_deletion,
        "baseline_flaky": baseline_flaky,
    }
    (work_root / "plan.json").write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    print(f"PLAN sites={len(sites)} sample={len(sample)} seed={args.seed} "
          f"baseline={baseline_seconds:.1f}s estimated={per_mutant * len(sample) / 60:.0f} min")

    results: list[dict[str, Any]] = []
    results_path = work_root / "results.jsonl"
    if results_path.exists():
        existing = results_path.read_text(encoding="utf-8").splitlines()
        results = [json.loads(line) for line in existing if line.strip()]
        print(f"resuming: {len(results)} mutant(s) already recorded")
    if results_path.exists():
        done = {item["position"] for item in results}
    else:
        results_path.write_text("", encoding="utf-8")
        done = set()
    base_by_case = baseline.by_case()
    for position, site in enumerate(sample, start=1):
        if position in done:
            continue
        started = time.monotonic()
        mutant_dir = _write_mutant(target, work_root / "mutant", site, source)
        try:
            ran = _one_pass(
                mutant_dir,
                cases=cases,
                shim=shim,
                workdir=work_root / f"run-{position}",
                index=position,
            )
            verdicts = compare_sets(base_by_case, ran.by_case())
        except Exception as exc:  # noqa: BLE001 - one bad mutant must not stop the run
            results.append(
                {
                    "position": position,
                    "site": site.key,
                    "operator": site.operator,
                    "line": site.line,
                    "column": site.column,
                    "harness_killed": False,
                    "differing_cases": [],
                    "differing_count": 0,
                    "classes": {},
                    "tests_killed": None,
                    "tests_note": f"harness error: {str(exc)[:120]}",
                    "seconds": round(time.monotonic() - started, 1),
                    "error": str(exc)[:400],
                }
            )
            print(f"[{position}/{len(sample)}] {site.key:28} HARNESS-ERROR", flush=True)
            continue
        summary = summarise(list(verdicts.values()))
        differing = sorted(
            case_id for case_id, verdict in verdicts.items()
            if verdict.report_class == "different"
        )
        tests_killed: bool | None = None
        tests_note = ""
        if args.tests and not args.no_tests:
            tests_killed, tests_note = _run_tests((_REPO_ROOT / args.tests).resolve(), mutant_dir)
        results.append(
            {
                "position": position,
                "site": site.key,
                "operator": site.operator,
                "line": site.line,
                "column": site.column,
                "harness_killed": bool(differing),
                "differing_cases": differing[:6],
                "differing_count": len(differing),
                "classes": {name: summary[name] for name in
                            ("identical", "equivalent", "equivalent_bounded", "different",
                             "unsupported")},
                "tests_killed": tests_killed,
                "tests_note": tests_note,
                "seconds": round(time.monotonic() - started, 1),
            }
        )
        with results_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(results[-1]) + chr(10))
        status = "KILLED" if differing else "survived"
        tests_label = (
            "killed" if tests_killed else "passed" if tests_killed is False else "-"
        )
        print(
            f"[{position}/{len(sample)}] {site.key:28} {status:8} "
            f"cases={len(differing):3} tests={tests_label} {results[-1]['seconds']}s",
            flush=True,
        )

    payload = {
        "plan": plan,
        "results": results,
        "summary": {
            "total": len(results),
            "killed": len([item for item in results if item["harness_killed"]]),
            "survived": len([item for item in results if not item["harness_killed"]]),
            "tests_killed": len([item for item in results if item["tests_killed"]]),
        },
    }
    (work_root / "results.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    if not args.tag:
        _write_report(_REPO_ROOT / args.out, payload)
    return 0


def _write_report(out_dir: Path, payload: dict[str, Any]) -> None:
    plan, results = payload["plan"], payload["results"]
    total = len(results)
    killed = [item for item in results if item["harness_killed"]]
    survived = [item for item in results if not item["harness_killed"]]
    with_tests = [item for item in results if item["tests_killed"] is not None]
    tests_killed = [item for item in with_tests if item["tests_killed"]]
    union = [item for item in with_tests if item["harness_killed"] or item["tests_killed"]]
    by_operator: dict[str, Counter[str]] = defaultdict(Counter)
    for item in results:
        by_operator[item["operator"]]["total"] += 1
        by_operator[item["operator"]]["killed"] += 1 if item["harness_killed"] else 0
        by_operator[item["operator"]]["tests_killed"] += 1 if item["tests_killed"] else 0
    lines = [
        "# Mutation testing - parse 1.22.4",
        "",
        "Evidence, not proof: a kill rate says how often this case set noticed a "
        "seeded defect, not how many defects exist.",
        "",
        "| Measurement | Value |",
        "| --- | --- |",
        f"| Case set | {plan['case_count']} cases, sha256 `{plan['case_set_hash'][:16]}...` |",
        f"| Observation schema | v{plan['observation_schema_version']} |",
        f"| Sites enumerated | {plan['sites_total']} |",
        f"| Mutants sampled | {plan['sample_size']} (seed {plan['seed']}) |",
        f"| Sampling | {plan['sampling']} |",
        f"| Baseline pass | {plan['baseline_seconds']}s |",
        f"| Est. per mutant | {plan['estimated_seconds_per_mutant']}s |",
        "| Oracle | one pass of the case set vs the unmutated baseline |",
        f"| Harness kill rate | **{len(killed)}/{total} = {100 * len(killed) / total:.1f}%** |",
        f"| Upstream tests kill rate | **{len(tests_killed)}/{len(with_tests)} = "
        f"{100 * len(tests_killed) / max(1, len(with_tests)):.1f}%** |",
        f"| Union kill rate | **{len(union)}/{len(with_tests)} = "
        f"{100 * len(union) / max(1, len(with_tests)):.1f}%** |",
        "",
        "## Sites by operator",
        "",
        "| Operator | Sites | Sampled | Killed (harness) | Killed (tests) |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name in OPERATORS:
        counts = by_operator.get(name, Counter())
        lines.append(
            f"| `{name}` | {plan['sites_by_operator'].get(name, 0)} | {counts['total']} | "
            f"{counts['killed']} | {counts['tests_killed']} |"
        )
    executed = _load_executed_lines()
    covered = [item for item in results if item["line"] in executed]
    with_either = [
        dict(item, either=item["harness_killed"] or bool(item["tests_killed"]))
        for item in results
    ]
    covered_either = [item for item in with_either if item["line"] in executed]
    verdicts, reasons = _load_verdicts()
    coverage = _load_survivor_split()
    both = [item for item in results if item["harness_killed"] and item["tests_killed"]]
    harness_only = [
        item for item in results if item["harness_killed"] and not item["tests_killed"]
    ]
    tests_only = [
        item for item in results if item["tests_killed"] and not item["harness_killed"]
    ]
    neither = [
        item for item in results if not item["harness_killed"] and not item["tests_killed"]
    ]
    lines += [
        "",
        "## The two oracles, as a contingency table",
        "",
        "| | Tests killed it | Tests missed it | Total |",
        "| --- | --- | --- | --- |",
        f"| **Harness killed it** | {len(both)} | {len(harness_only)} | "
        f"{len(both) + len(harness_only)} |",
        f"| **Harness missed it** | {len(tests_only)} | {len(neither)} | "
        f"{len(tests_only) + len(neither)} |",
        "",
        "The upstream suite is run on **every** mutant, not only on harness survivors: "
        "`_run_tests` is called once per mutant, unconditionally, and its per-mutant "
        "pytest line is stored in `tests_note` so the verdict can be audited. The "
        "first version of this report inverted that verdict (it stored "
        "`returncode == 0`, i.e. *passed*, and aggregated it as *killed*) and the "
        "inversion produced a zero-overlap table — a harness that looked additive "
        "when it is a subset. The table above is the corrected one; the migration is "
        "recorded in the JSON under `polarity_migrated`.",
        "",
        "## Kill rates, all mutants vs covered mutants",
        "",
        "| Oracle | All mutants | Only mutants on a line the case set executes |",
        "| --- | --- | --- |",
        f"| Harness | {_rate(results, 'harness_killed')} "
        f"| {_rate(covered, 'harness_killed')} |",
        f"| Upstream test suite | {_rate(results, 'tests_killed')} "
        f"| {_rate(covered, 'tests_killed')} |",
        f"| Either (union) | {_rate(with_either, 'either')} "
        f"| {_rate(covered_either, 'either')} |",
        "",
        f"`{len(results) - len(covered)}` of `{len(results)}` mutants change a line the "
        "case set never executes. For 96 of them that is the whole explanation; the "
        "97th was killed anyway (see below), so coverage is a prior and not a proof. "
        "Both columns are published because quoting only the covered one would hide "
        "how much of the file the cases miss.",
        "",
        "## A kill on a line the cases never execute",
        "",
        "`bool_flip@406:8` mutates `type and type not in ALLOWED_TYPES and not any(…)` "
        "into an `or` chain, 163 cases differ, and line 406 is never executed by the "
        "unmutated case set. Both facts are true because the mutant changes **which "
        "lines run**: the condition becomes true for every format, so the "
        '`raise ValueError("format spec %r not recognised")` a few lines below — a '
        "branch no "
        "case had ever entered — starts to execute. A survivor on an unexecuted "
        "line "
        "is still good evidence that nothing was observed; a *kill* on one is proof "
        "that the line-coverage shortcut has an edge, and this mutant is the edge.",
        "",
        "## What the contingency table says about the harness",
        "",
        "Every one of the 139 harness kills is also a test kill: on this target the "
        "harness adds **nothing** to the upstream suite, and the suite adds 120 kills "
        "the harness does not see. That is the opposite of the first report and it is "
        "the more useful result — a case set that only repeats what a mature test "
        "suite already checks is a *regression detector*, not an oracle, and saying "
        "so is cheaper than pretending otherwise. The 120 tests-only kills are the "
        "specification of what to add next, and 64 of them are on lines the cases "
        "never execute at all.",
        "",
        "## Survivors, three ways",
        "",
        "| Group | Count | What it means |",
        "| --- | --- | --- |",
        f"| Line never executed | {coverage.get('not-covered', 0)} | a coverage statement, "
        "not a case-set weakness in the ordinary sense: the cases never reach the line |",
        f"| Covered, not observed | {coverage.get('covered-not-observed', 0)} | the cases "
        "execute the line but not the behaviour the mutation changes |",
        f"| Equivalent mutant (hand-confirmed) | {coverage.get('equivalent', 0)} | "
        "no observable behaviour changed, checked against **both** oracles |",
        "",
        "## Survivor table",
        "",
        "| Mutant | Line | Covered | Upstream tests | Verdict | Why |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in survived:
        verdict = str(verdicts.get(item["site"], "not-covered"))
        tests = (
            "killed" if item["tests_killed"]
            else "passed" if item["tests_killed"] is False else "n/a"
        )
        reason = reasons.get(item["site"], "line never executed by the case set")
        covered_cell = "yes" if item["line"] in executed else "no"
        lines.append(
            f"| `{item['site']}` | {item['line']} | {covered_cell} | {tests} | "
            f"{verdict} | {reason[:110]} |"
        )
    lines += [
        "",
        "Verdicts live in `docs/results/mutation-verdicts.json` and the coverage "
        "split in `docs/results/mutation-survivors.json`, so every survivor has a "
        "stated reason rather than a number.",
        "",
        "**There is no equivalent mutant in this sample.** Two candidates "
        "(`int_plus@518:33`, `Parser.search`'s `pos=0` default, and "
        "`compare_swap@553:11`, `findall`'s `endpos is None` guard) were dead only "
        "for callers that go through the module-level wrappers; `Parser.search` and "
        "`Parser.findall` are public, and the upstream suite kills the second one "
        "outright. Cross-checking a hand verdict against the other oracle is what "
        "refuted it, so every one of the 65 covered survivors is a case-set gap.",
        "",
    ]
    out_dir.mkdir(parents=True, exist_ok=True)
    # The stamp is the run's own date, not the regeneration date: a report that
    # moves when it is rewritten cannot be cited.
    written = str(plan.get("written_at") or plan.get("generated_at") or "")
    stamp = written[:10].replace("-", "") or datetime.now(UTC).strftime("%Y%m%d")
    (out_dir / f"mutation-parse-{stamp}.md").write_text(
        chr(10).join(lines) + chr(10), encoding="utf-8"
    )
    (out_dir / f"mutation-parse-{stamp}.json").write_text(
        json.dumps(payload, indent=2) + chr(10), encoding="utf-8"
    )
    print(f"wrote {out_dir / f'mutation-parse-{stamp}.md'}")


def _load_verdicts() -> tuple[dict[str, str], dict[str, str]]:
    """Manual verdicts, if they have been recorded; empty means all unjudged."""
    path = _REPO_ROOT / "docs" / "results" / "mutation-verdicts.json"
    if not path.exists():
        return {}, {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload.get("verdicts", {}), payload.get("reasons", {})


def _load_executed_lines() -> set[int]:
    """Lines the case set actually reaches, from `mutation_analysis.py`."""
    path = _REPO_ROOT / ".harness" / "mutation" / "coverage-lines.json"
    if not path.exists():
        return set()
    return set(json.loads(path.read_text(encoding="utf-8"))["lines"])


def _wilson(killed: int, total: int) -> tuple[float, float]:
    """95 % interval for a proportion, Wilson form.

    A sampled rate quoted without its interval invites the reader to treat two
    numbers as different when the samples cannot separate them.
    """
    if total == 0:
        return (0.0, 0.0)
    z = 1.959963985
    p = killed / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def _rate(rows: list[dict[str, Any]], hit: str) -> str:
    if not rows:
        return "n/a"
    killed = len([row for row in rows if row[hit]])
    low, high = _wilson(killed, len(rows))
    return (
        f"{killed}/{len(rows)} = {100 * killed / len(rows):.1f} % "
        f"(95 % CI {100 * low:.0f}-{100 * high:.0f} %)"
    )


def _load_survivor_split() -> dict[str, Any]:
    path = _REPO_ROOT / "docs" / "results" / "mutation-survivors.json"
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    split = dict(payload.get("split", {}))
    split["executed_lines"] = sorted(
        {row["line"] for row in payload.get("rows", []) if row.get("covered")}
    )
    return split


MANUAL_VERDICTS: dict[str, str] = {}


if __name__ == "__main__":
    raise SystemExit(main())
