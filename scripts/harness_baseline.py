"""Checkpoint 10 baseline for a stage-1 target: cases, coverage, A/A noise.

Produces, for one target version:

1. the **case set** (`harness/cases/<target>.json`), counted by source;
2. **coverage** of the target under the case set, with the uncovered functions
   listed — the honest limit of what the harness can say;
3. the **A/A baseline**: the same version run N times, any case whose
   observation differs between passes is noise;
4. the **shim's own proof**: a pass without the shim, to show which cases the
   controls actually decide;
5. a report (`docs/results/harness-<target>-<date>.md` + `.json`) and the
   golden observations as content-addressed blobs.

Usage:

    python scripts/harness_baseline.py --target parse --target-path .harness/target \
        --passes 5 --out docs/results

The target is imported from `--target-path`, never installed into the harness's
own environment, and every pass runs in a child interpreter with the shim.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from harness import determinism  # noqa: E402
from harness.cases import build_parse_cases, counts_by_source, save_cases  # noqa: E402
from harness.observation import BlobStore  # noqa: E402
from harness.runner import Pass, baseline_noise, digest_map, run_pass  # noqa: E402


def _target_version(target_path: Path, module: str) -> str:
    meta = target_path / f"{module}-{module}.dist-info" / "METADATA"
    candidates = list(target_path.glob(f"{module}-*.dist-info/METADATA"))
    if candidates:
        for line in candidates[0].read_text(encoding="utf-8").splitlines():
            if line.startswith("Version:"):
                return line.split(":", 1)[1].strip()
    return meta.parent.name if meta.exists() else "unknown"


def _uncovered_functions(target_path: Path, coverage_data: Path, module: str) -> list[str]:
    """Functions/methods with no executed line, from coverage's own JSON."""
    json_path = coverage_data.with_suffix(".json")
    subprocess.run(
        [sys.executable, "-m", "coverage", "json", "-o", str(json_path),
         f"--data-file={coverage_data}"],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
    )
    if not json_path.exists():
        return []
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    uncovered: list[str] = []
    for path, file_data in payload.get("files", {}).items():
        for name, function in (file_data.get("functions") or {}).items():
            if function.get("missing_lines"):
                uncovered.append(f"{Path(path).name}::{name}")
    return sorted(uncovered)


def _summary(pass_: Pass) -> dict[str, int]:
    counts = {"return": 0, "exception": 0, "unsupported": 0}
    for observation in pass_.observations:
        counts[observation.outcome] = counts.get(observation.outcome, 0) + 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default="parse")
    parser.add_argument("--target-path", default=".harness/target")
    parser.add_argument("--passes", type=int, default=5)
    parser.add_argument("--hypothesis-examples", type=int, default=40)
    parser.add_argument("--out", default="docs/results")
    args = parser.parse_args()

    target_path = (_REPO_ROOT / args.target_path).resolve()
    checkout = target_path  # the target's own tree is the working directory
    version = _target_version(target_path, args.target)
    out_dir = (_REPO_ROOT / args.out)
    work = _REPO_ROOT / ".harness" / "work"
    shim_dir = determinism.write_shim(_REPO_ROOT / ".harness" / "shim")
    cases_path = _REPO_ROOT / "harness" / "cases" / f"{args.target}.json"

    cases = build_parse_cases(hypothesis_examples=args.hypothesis_examples)
    save_cases(cases_path, cases, args.target, version)
    print(f"OBSERVED cases: {len(cases)} {counts_by_source(cases)}")

    def _run(index: int, *, shim: Path, coverage: bool = False) -> Pass:
        return run_pass(
            python=sys.executable,
            checkout=checkout,
            shim_dir=shim,
            src_dir=_REPO_ROOT / "src",
            target_paths=[target_path],
            cases=cases,
            index=index,
            module=args.target,
            workdir=work,
            coverage=coverage,
        )

    passes = [_run(index, shim=shim_dir) for index in range(1, args.passes + 1)]
    noise = baseline_noise(passes)
    print(f"OBSERVED A/A noise: {len(noise)} case(s) varied over {args.passes} passes")

    # the shim's own proof: same code, same cases, WITHOUT the controls
    unshimmed_dir = _REPO_ROOT / ".harness" / "no-shim"
    unshimmed_dir.mkdir(parents=True, exist_ok=True)
    unshimmed = _run(args.passes + 1, shim=unshimmed_dir)
    reference = passes[0].by_case()
    shim_dependent = sorted(
        case_id
        for case_id, observation in unshimmed.by_case().items()
        if case_id in reference and reference[case_id].fingerprint != observation.fingerprint
    )
    print(f"OBSERVED cases the shim decides: {len(shim_dependent)} {shim_dependent[:6]}")

    coverage_data = work / "coverage.data"
    _run(0, shim=shim_dir, coverage=True)
    uncovered = _uncovered_functions(target_path, coverage_data, args.target)
    print(f"OBSERVED uncovered functions: {len(uncovered)}")

    store = BlobStore(out_dir / f"harness-{args.target}-blobs")
    for observation in passes[0].observations:
        store.put(observation)

    today = datetime.now(UTC).strftime("%Y%m%d")
    report = {
        "target": args.target,
        "version": version,
        "generated_at": datetime.now(UTC).isoformat(),
        "case_count": len(cases),
        "cases_by_source": counts_by_source(cases),
        "outcomes": _summary(passes[0]),
        "passes": args.passes,
        "controls": list(passes[0].controls),
        "aa_noise": noise,
        "shim_dependent_cases": shim_dependent,
        "uncovered_functions": uncovered,
        "golden_blobs": len(store.written),
        "digests": digest_map(passes[0]),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"harness-{args.target}-{today}.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _write_markdown(out_dir / f"harness-{args.target}-{today}.md", report)
    print(f"wrote {out_dir / f'harness-{args.target}-{today}.md'}")
    return 0


def _write_markdown(path: Path, report: dict) -> None:
    lines = [
        f"# Harness baseline — {report['target']} {report['version']}",
        "",
        "Evidence, not proof: this records what the target did under a fixed "
        "case set and a determinism shim. It says nothing about behaviour the "
        "cases never reach, and the coverage section below is the honest limit.",
        "",
        "| Measurement | Value |",
        "| --- | --- |",
        f"| Cases | {report['case_count']} |",
        f"| Outcomes (return / exception / unsupported) | {report['outcomes']} |",
        f"| A/A passes | {report['passes']} |",
        f"| A/A noise (cases that varied) | **{len(report['aa_noise'])}** |",
        f"| Cases decided by the shim | {len(report['shim_dependent_cases'])} |",
        f"| Uncovered functions | {len(report['uncovered_functions'])} |",
        f"| Golden blobs | {report['golden_blobs']} |",
        f"| Controls applied | {', '.join(report['controls'])} |",
        "",
        "## Cases by source",
        "",
        "| Source | Cases |",
        "| --- | --- |",
    ]
    lines += [f"| {source} | {count} |" for source, count in report["cases_by_source"].items()]
    lines += ["", "## A/A noise report", ""]
    lines.append(
        "No case varied across passes." if not report["aa_noise"]
        else "\n".join(f"- `{item['case_id']}` — {item['kind']}" for item in report["aa_noise"])
    )
    lines += ["", "## Cases the determinism shim decides", ""]
    lines.append(
        "\n".join(f"- `{case_id}`" for case_id in report["shim_dependent_cases"])
        or "None: every case is clock-, random- and hash-independent."
    )
    lines += ["", "## Uncovered functions (the limit of these claims)", ""]
    lines.append(
        "\n".join(f"- `{name}`" for name in report["uncovered_functions"])
        or "Every function was reached at least once."
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
