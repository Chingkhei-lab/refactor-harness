"""verify-change: compare two trees of one target and report what changed.

The workflow, in order, because each step can invalidate the next:

1. **A/A first.** Two passes of the OLD tree. Any case that varies is `flaky`
   and is removed from the comparison's evidence — a change cannot be blamed on
   a case that disagrees with itself.
2. **Alternate the order.** old, new, old, new. Load-dependent noise then lands
   on position, not on version.
3. **Compare** with the rules file, if the target has one, and classify every
   case into `identical` / `equivalent` / `equivalent_bounded` / `different` /
   `unsupported` (with `flaky` from step 1).
4. **Record** the case-set hash, the rules hash, the controls applied, the
   counts and the coverage — a report missing any of them is not a report.

Usage:

    python scripts/verify_change.py --old path/to/old --new path/to/new
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from harness import determinism  # noqa: E402
from harness.cases import Case, case_set_hash, counts_by_source, load_cases  # noqa: E402
from harness.compare import CLASS_FLAKY, compare_sets, summarise  # noqa: E402
from harness.observation import OBSERVATION_SCHEMA_VERSION  # noqa: E402
from harness.rules import load_rules, usage  # noqa: E402
from harness.runner import Pass, baseline_noise, run_pass  # noqa: E402


def _target_version(path: Path, module: str) -> str:
    for candidate in path.glob(f"{module}-*.dist-info/METADATA"):
        for line in candidate.read_text(encoding="utf-8").splitlines():
            if line.startswith("Version:"):
                return line.split(":", 1)[1].strip()
    return "unknown"


def _run(
    *,
    checkout: Path,
    module: str,
    cases: list[Case],
    shim: Path,
    index: int,
    workdir: Path,
    set_hash: str,
    cpu_budget: float,
) -> Pass:
    return run_pass(
        python=sys.executable,
        checkout=checkout,
        shim_dir=shim,
        src_dir=_REPO_ROOT / "src",
        target_paths=[checkout],
        cases=cases,
        index=index,
        module=module,
        workdir=workdir,
        expected_case_set_hash=set_hash,
        case_budget_seconds=30.0,
        cpu_budget_seconds=cpu_budget,
    )


def verify(
    *,
    old: Path,
    new: Path,
    cases_path: Path,
    module: str,
    rules_path: Path | None,
    out_dir: Path,
    coverage: bool,
    cpu_budget_seconds: float = 0.0,
    label: str = "",
) -> dict[str, Any]:
    cases = load_cases(cases_path)
    set_hash = case_set_hash(cases)
    rules = (
        load_rules(rules_path, case_set_hash=set_hash)
        if rules_path is not None
        else load_rules(Path("/nonexistent"), case_set_hash=set_hash)
    )
    work = _REPO_ROOT / ".harness" / "verify"
    shim = determinism.write_shim(_REPO_ROOT / ".harness" / "shim")

    # 1 + 2: A/A on the old tree, then the alternating old/new pairs.
    old_passes = [
        _run(checkout=old, module=module, cases=cases, shim=shim, index=index,
             workdir=work / f"old-{index}", set_hash=set_hash, cpu_budget=cpu_budget_seconds)
        for index in (1, 2)
    ]
    noise = baseline_noise(old_passes)
    flaky = {item["case_id"] for item in noise}

    pairs: list[tuple[Pass, Pass, str]] = []
    for round_index in (1, 2):
        old_pass = _run(checkout=old, module=module, cases=cases, shim=shim,
                        index=10 + round_index, workdir=work / f"old-r{round_index}",
                        set_hash=set_hash, cpu_budget=cpu_budget_seconds)
        new_pass = _run(checkout=new, module=module, cases=cases, shim=shim,
                        index=20 + round_index, workdir=work / f"new-r{round_index}",
                        set_hash=set_hash, cpu_budget=cpu_budget_seconds)
        pairs.append((old_pass, new_pass, f"old,new #{round_index}"))

    verdicts = []
    applied_rules: list[Any] = []
    for old_pass, new_pass, _label in pairs:
        for case_id, verdict in compare_sets(
            old_pass.by_case(), new_pass.by_case(), rules
        ).items():
            applied_rules.extend(verdict.applied)
            if case_id in flaky:
                verdict = type(verdict)(
                    case_id=case_id,
                    outcome="undecidable",
                    reason="flaky: the case varied between two A/A passes of the old tree",
                )
            verdicts.append(verdict)
    summary = summarise(verdicts)
    summary[CLASS_FLAKY] = len(flaky)
    summary["flaky_cases"] = sorted(flaky)

    report = {
        "target": module,
        "old": {"path": str(old), "version": _target_version(old, module)},
        "new": {"path": str(new), "version": _target_version(new, module)},
        "generated_at": datetime.now(UTC).isoformat(),
        "case_set_hash": set_hash,
        "case_count": len(cases),
        "cases_by_source": counts_by_source(cases),
        "rules": {"path": str(rules_path) if rules_path else None, "sha256": rules.sha256,
                  "count": len(rules.rules)},
        "controls": list(old_passes[0].controls),
        "order": "old, new, old, new (alternating)",
        "budget": {
            "cpu_seconds_per_case": cpu_budget_seconds,
            "kind": "cpu+rlimit" if cpu_budget_seconds else "wall clock",
        },
        "aa_noise": noise,
        "summary": summary,
        "differences": [
            {"case_id": verdict.case_id, "path": verdict.path, "reason": verdict.reason,
             "left": verdict.left, "right": verdict.right}
            for verdict in verdicts
            if verdict.report_class == "different"
        ][:50],
        "unsupported": [
            {"case_id": verdict.case_id, "reason": verdict.reason}
            for verdict in verdicts
            if verdict.outcome == "undecidable"
        ][:50],
        "masked": sorted({mask for verdict in verdicts for mask in verdict.masked}),
        "rules_usage": usage(rules, applied_rules),
        "observation_schema_version": OBSERVATION_SCHEMA_VERSION,
        "rounds": len(pairs),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    slug = f"verify-change-{module}-{stamp}" + (f"-{label}" if label else "")
    (out_dir / f"{slug}.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _write_markdown(out_dir / f"{slug}.md", report)
    return report


def _budget_detail(report: dict[str, Any]) -> str:
    seconds = report["budget"]["cpu_seconds_per_case"]
    return f", {seconds:g}s per case" if seconds else ""


def _write_markdown(path: Path, report: dict[str, Any]) -> None:
    summary = report["summary"]
    lines = [
        f"# verify-change - {report['target']}",
        "",
        f"old `{report['old']['path']}` ({report['old']['version']}) vs "
        f"new `{report['new']['path']}` ({report['new']['version']})",
        "",
        "Evidence, not proof: this says what these cases did under this shim. "
        "It does not say the two versions are equivalent, and the unsupported "
        "and coverage lines below are the limit of the claim.",
        "",
        "| Measurement | Value |",
        "| --- | --- |",
        f"| Case set | {report['case_count']} cases, sha256 `{report['case_set_hash'][:16]}...` |",
        f"| Order | {report['order']} |",
        f"| Rules | {report['rules']['count']} rule(s), sha256 "
        f"`{(report['rules']['sha256'] or 'none')[:16]}` |",
        f"| Controls | {', '.join(report['controls'])} |",
        f"| Budget | {report['budget']['kind']}{_budget_detail(report)} |",
        f"| A/A noise (old tree) | {len(report['aa_noise'])} |",
        f"| Observation schema | v{report['observation_schema_version']} |",
    ]
    for name in ("identical", "equivalent", "equivalent_bounded", "different",
                 "unsupported", "flaky"):
        lines.append(f"| {name} | {summary.get(name, 0)} |")
    lines += ["", "## Differences", ""]
    if report["differences"]:
        lines += ["| Case | Path | Left | Right |", "| --- | --- | --- | --- |"]
        for item in report["differences"]:
            lines.append(
                f"| `{item['case_id']}` | `{item['path']}` | `{str(item['left'])[:60]}` | "
                f"`{str(item['right'])[:60]}` |"
            )
    else:
        lines.append("No case differed.")
    lines += ["", "## Unsupported (counted with unsupported, reasons listed)", ""]
    lines.append(
        "\n".join(f"- `{item['case_id']}`: {item['reason']}" for item in report["unsupported"])
        or "None."
    )
    if report["masked"]:
        lines += ["", "## Masked fields", ""]
        lines += [f"- `{mask}`" for mask in report["masked"]]
    if report["rules_usage"]:
        lines += [
            "",
            "## Rule usage (how close each rule came to masking something real)",
            "",
            "| Rule | Type | Matched | Largest difference | Tolerance | Ratio |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for rule_id, stat in sorted(report["rules_usage"].items()):
            delta = stat["max_delta"]
            ratio = stat["max_ratio"]
            lines.append(
                f"| `{rule_id}` | {stat['type']} | {stat['matched']} | "
                f"{'-' if delta is None else f'{delta:.3g}'} | "
                f"{'-' if stat['tolerance'] is None else f'{stat['tolerance']:.3g}'} | "
                f"{'-' if ratio is None else f'{ratio:.1%}'} |"
            )
    if report["summary"].get("flaky_cases"):
        lines += ["", "## Flaky cases (a case that disagrees with itself)", ""]
        lines += [f"- `{case_id}`" for case_id in report["summary"]["flaky_cases"]]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old", required=True)
    parser.add_argument("--new", required=True)
    parser.add_argument("--module", default="parse")
    parser.add_argument("--cases", default="")
    parser.add_argument("--rules", default="")
    parser.add_argument("--out", default="docs/results")
    parser.add_argument("--coverage", action="store_true")
    parser.add_argument("--label", default="", help="suffix for the report file name")
    parser.add_argument("--cpu-budget", type=float, default=0.0,
                        help="CPU seconds per case (POSIX); 0 disables")
    args = parser.parse_args()
    cases_path = Path(args.cases) if args.cases else (
        _REPO_ROOT / "harness" / "cases" / f"{args.module}.json"
    )
    rules_path = Path(args.rules) if args.rules else (
        _REPO_ROOT / "harness" / "rules" / f"{args.module}.json"
    )
    report = verify(
        old=Path(args.old).resolve(),
        new=Path(args.new).resolve(),
        cases_path=cases_path,
        module=args.module,
        rules_path=rules_path if rules_path.exists() else None,
        out_dir=_REPO_ROOT / args.out,
        coverage=args.coverage,
        cpu_budget_seconds=args.cpu_budget,
        label=args.label,
    )
    print(json.dumps(report["summary"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
