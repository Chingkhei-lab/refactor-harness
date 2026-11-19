"""Checkpoint 10 baseline for a stage-1 target: cases, coverage, A/A noise.

Produces, for one target version:

1. the **case set** (`harness/cases/<target>.json`) plus its sha256 — the hash
   goes into every observation, every blob path and this report;
2. **coverage** of the target (line AND branch), with the uncovered functions
   and the total function count — the honest limit of these claims;
3. the **A/A baseline in two orders**: fixed and shuffled. The shuffled run is
   the one that can expose cross-case coupling (random/uuid state carried from
   one case to the next), which a fixed order cannot;
4. **shim-off versus shim-on**, per clock surface, so "which cases the shim
   decides" is a method rather than a claim;
5. counts of **opaque** (no stable identity) and **truncated** (bounded
   iterator) observations, both of which are classified unsupported;
6. a report and the golden observations as content-addressed blobs.

Usage:

    python scripts/harness_baseline.py --target parse --target-path .harness/target
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from harness import determinism  # noqa: E402
from harness.cases import (  # noqa: E402
    build_parse_cases,
    case_set_hash,
    counts_by_source,
    save_cases,
)
from harness.observation import BlobStore  # noqa: E402
from harness.runner import Pass, baseline_noise, digest_map, run_pass  # noqa: E402


def _target_version(target_path: Path, module: str) -> str:
    candidates = list(target_path.glob(f"{module}-*.dist-info/METADATA"))
    if candidates:
        for line in candidates[0].read_text(encoding="utf-8").splitlines():
            if line.startswith("Version:"):
                return line.split(":", 1)[1].strip()
    return "unknown"


def _coverage(target_path: Path, coverage_data: Path, module: str) -> dict:
    """Line and branch coverage, plus the uncovered functions and the total."""
    json_path = coverage_data.with_suffix(".json")
    subprocess.run(
        [sys.executable, "-m", "coverage", "json", "-o", str(json_path),
         f"--data-file={coverage_data}"],
        cwd=str(_REPO_ROOT), capture_output=True, text=True,
    )
    result: dict = {
        "line_percent": None, "branch_percent": None, "statements": 0,
        "covered_statements": 0, "branches": 0, "covered_branches": 0,
        "uncovered_functions": [], "function_count": 0,
    }
    if not json_path.exists():
        return result
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    totals = payload.get("totals", {})
    result["statements"] = totals.get("num_statements", 0)
    result["covered_statements"] = totals.get("covered_lines", 0)
    result["branches"] = totals.get("num_branches", 0)
    result["covered_branches"] = totals.get("covered_branches", 0)
    result["line_percent"] = round(totals.get("percent_covered", 0.0), 2)
    result["branch_percent"] = (
        round(100.0 * result["covered_branches"] / result["branches"], 2)
        if result["branches"] else None
    )
    uncovered: list[str] = []
    count = 0
    for path, file_data in payload.get("files", {}).items():
        for name, function in (file_data.get("functions") or {}).items():
            count += 1
            if function.get("missing_lines"):
                uncovered.append(f"{Path(path).name}::{name}")
    result["uncovered_functions"] = sorted(uncovered)
    result["function_count"] = count
    return result


def _flag_counts(pass_: Pass) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for observation in pass_.observations:
        counts["opaque"] += 1 if observation.flags.get("opaque") else 0
        counts["truncated"] += 1 if observation.flags.get("truncated") else 0
        counts["limit_hit"] += 1 if observation.flags.get("limit") else 0
    return dict(counts)


def _summary(pass_: Pass) -> dict[str, int]:
    counts: Counter[str] = Counter(observation.outcome for observation in pass_.observations)
    return dict(counts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default="parse")
    parser.add_argument("--target-path", default=".harness/target")
    parser.add_argument("--passes", type=int, default=5)
    parser.add_argument("--hypothesis-examples", type=int, default=40)
    parser.add_argument("--out", default="docs/results")
    args = parser.parse_args()

    target_path = (_REPO_ROOT / args.target_path).resolve()
    version = _target_version(target_path, args.target)
    out_dir = _REPO_ROOT / args.out
    work = _REPO_ROOT / ".harness" / "work"
    shim_dir = determinism.write_shim(_REPO_ROOT / ".harness" / "shim")
    cases_path = _REPO_ROOT / "harness" / "cases" / f"{args.target}.json"

    cases = build_parse_cases(hypothesis_examples=args.hypothesis_examples)
    save_cases(cases_path, cases, args.target, version)
    set_hash = case_set_hash(cases)
    print(f"OBSERVED cases: {len(cases)} {counts_by_source(cases)} hash={set_hash[:12]}")

    def _run(index: int, *, shim: Path, order: str = "fixed", coverage: bool = False) -> Pass:
        return run_pass(
            python=sys.executable,
            checkout=target_path,
            shim_dir=shim,
            src_dir=_REPO_ROOT / "src",
            target_paths=[target_path],
            cases=cases,
            index=index,
            module=args.target,
            workdir=work,
            coverage=coverage,
            order=order,
            shuffle_seed=7,
            expected_case_set_hash=set_hash,
        )

    fixed = [_run(index, shim=shim_dir) for index in range(1, args.passes + 1)]
    fixed_noise = baseline_noise(fixed)
    print(f"OBSERVED A/A noise (fixed order): {len(fixed_noise)}")

    shuffled = [
        _run(100 + index, shim=shim_dir, order="shuffled")
        for index in range(1, args.passes + 1)
    ]
    shuffled_noise = baseline_noise(shuffled)
    print(f"OBSERVED A/A noise (shuffled order): {len(shuffled_noise)}")

    # shim-off, one pass, to say WHICH clock surfaces the controls decide
    bare_dir = _REPO_ROOT / ".harness" / "no-shim"
    bare_dir.mkdir(parents=True, exist_ok=True)
    unshimmed = _run(args.passes + 100, shim=bare_dir)
    reference = fixed[0].by_case()
    shim_dependent = sorted(
        case_id
        for case_id, observation in unshimmed.by_case().items()
        if case_id in reference and reference[case_id].fingerprint != observation.fingerprint
    )
    print(f"OBSERVED cases decided by the shim: {len(shim_dependent)}")

    probe_shimmed = determinism.run_probe(sys.executable, target_path, shim_dir, [target_path])
    probe_bare = determinism.run_probe(sys.executable, target_path, bare_dir, [target_path])
    shimmed_fields = determinism.parse_probe(probe_shimmed["stdout"])
    bare_fields = determinism.parse_probe(probe_bare["stdout"])
    clock_surfaces = {
        name: {
            "shimmed": shimmed_fields.get(name),
            "unshimmed": bare_fields.get(name),
            "controlled": shimmed_fields.get(name) != bare_fields.get(name),
        }
        for name in (name for name, _ in determinism.PROBE_LINES)
    }
    print(
        "OBSERVED clock surfaces controlled:",
        sorted(name for name, info in clock_surfaces.items() if info["controlled"]),
    )

    coverage_data = work / "coverage.data"
    _run(0, shim=shim_dir, coverage=True)
    coverage = _coverage(target_path, coverage_data, args.target)
    print(
        f"OBSERVED coverage: lines {coverage['covered_statements']}/{coverage['statements']} "
        f"({coverage['line_percent']}%), branches {coverage['covered_branches']}/"
        f"{coverage['branches']} ({coverage['branch_percent']}%), "
        f"uncovered functions {len(coverage['uncovered_functions'])}/{coverage['function_count']}"
    )

    store = BlobStore(out_dir / f"harness-{args.target}-blobs")
    for observation in fixed[0].observations:
        store.put(observation)

    today = datetime.now(UTC).strftime("%Y%m%d")
    report = {
        "target": args.target,
        "version": version,
        "generated_at": datetime.now(UTC).isoformat(),
        "case_set_hash": set_hash,
        "case_count": len(cases),
        "cases_by_source": counts_by_source(cases),
        "outcomes": _summary(fixed[0]),
        "flags": _flag_counts(fixed[0]),
        "passes": args.passes,
        "orders": {
            "fixed": {"noise": fixed_noise},
            "shuffled": {"noise": shuffled_noise},
        },
        "controls": list(fixed[0].controls),
        "clock_surfaces": clock_surfaces,
        "shim_dependent_cases": shim_dependent,
        "coverage": coverage,
        "golden_blobs": len(store.written),
        "digests": digest_map(fixed[0]),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"harness-{args.target}-{today}.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _write_markdown(out_dir / f"harness-{args.target}-{today}.md", report)
    print(f"wrote {out_dir / f'harness-{args.target}-{today}.md'}")
    return 0


def _write_markdown(path: Path, report: dict) -> None:
    coverage = report["coverage"]
    unfixed = report["orders"]["fixed"]["noise"]
    shuffled = report["orders"]["shuffled"]["noise"]
    lines = [
        f"# Harness baseline - {report['target']} {report['version']}",
        "",
        "Evidence, not proof: this records what the target did under a fixed case "
        "set and a determinism shim. It says nothing about behaviour the cases "
        "never reach, and the coverage section below is the honest limit.",
        "",
        "| Measurement | Value |",
        "| --- | --- |",
        f"| Case set | {report['case_count']} cases, sha256 `{report['case_set_hash'][:16]}...` |",
        f"| Outcomes | {report['outcomes']} |",
        f"| Opaque / truncated / limit-hit observations | {report['flags']} |",
        f"| A/A passes per order | {report['passes']} fixed, {report['passes']} shuffled |",
        f"| A/A noise (fixed order) | **{len(unfixed)}** |",
        f"| A/A noise (shuffled order) | **{len(shuffled)}** |",
        f"| Cases the shim decides | {len(report['shim_dependent_cases'])} |",
        f"| Line coverage | {coverage['covered_statements']}/{coverage['statements']} "
        f"({coverage['line_percent']}%) |",
        f"| Branch coverage | {coverage['covered_branches']}/{coverage['branches']} "
        f"({coverage['branch_percent']}%) |",
        f"| Uncovered functions | {len(coverage['uncovered_functions'])}/"
        f"{coverage['function_count']} |",
        f"| Golden blobs | {report['golden_blobs']} |",
        f"| Controls applied | {', '.join(report['controls'])} |",
        "",
        "## Cases by source",
        "",
        "| Source | Cases |",
        "| --- | --- |",
    ]
    lines += [f"| {source} | {count} |" for source, count in report["cases_by_source"].items()]
    lines += [
        "",
        "## Clock surfaces: which ones the shim actually controls",
        "",
        "Both arms are real runs of the probe: one with the shim module injected, "
        "one without. A surface is 'controlled' only if the two arms differ.",
        "",
        "`hash` shows as uncontrolled because it is fixed in the *environment* "
        "(`PYTHONHASHSEED=0`, read before any of our code runs) rather than by the "
        "injected module, so both arms agree. It is still controlled; the probe "
        "just cannot see the difference.",
        "",
        "| Surface | Shimmed | Unshimmed | Controlled |",
        "| --- | --- | --- | --- |",
    ]
    for name, info in report["clock_surfaces"].items():
        controlled = "yes" if info["controlled"] else "**no**"
        lines.append(f"| `{name}` | `{info['shimmed']}` | `{info['unshimmed']}` | {controlled} |")
    lines += ["", "## A/A noise", ""]
    for order in ("fixed", "shuffled"):
        noise = report["orders"][order]["noise"]
        detail = (
            "no case varied across passes."
            if not noise
            else ", ".join(f"`{item['case_id']}` ({item['kind']})" for item in noise)
        )
        lines.append(f"- **{order} order**: {detail}")
    lines += [
        "",
        "## Cases the determinism shim decides",
        "",
        "Method: run one pass with the shim and one without, then compare the "
        "control-free fingerprint of each case. A case that differs is one where "
        "a shimmed surface changed the observed value; the clock-surface table "
        "above says which surfaces those are.",
        "",
    ]
    shim_cases = chr(10).join(f"- `{case_id}`" for case_id in report["shim_dependent_cases"])
    lines.append(shim_cases or "None: every case is clock-, random- and hash-independent.")
    lines += [
        "",
        "## Known target defect (reproduced outside the harness)",
        "",
        "`parse.findall('{:tt}', '22:13:20')` never terminates: after the full "
        "match it keeps yielding the same zero-width match at offset 8. "
        "Reproduced with no shim, no sandbox and an external timeout, which "
        "exits 124. It is NOT new: `parse` 1.22.4 is the latest upstream release "
        "and issue #246 ('Avoid infinite findall iteration for empty matches', "
        "open since 2026-09-12) tracks it. The harness records the case as a "
        "truncated observation (1 000 items, `truncated: true`) instead of "
        "hanging.",
        "",
        "## Uncovered functions (the limit of these claims)",
        "",
        f"{len(coverage['uncovered_functions'])} of {coverage['function_count']} "
        "functions were never executed by the case set:",
        "",
    ]
    uncovered = [f"- `{name}`" for name in coverage["uncovered_functions"]]
    lines.append(chr(10).join(uncovered) or "Every function was reached at least once.")
    path.write_text(chr(10).join(lines) + chr(10), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
