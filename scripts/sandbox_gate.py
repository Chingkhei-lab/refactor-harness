"""The sandbox regression gate: same verdicts, same bytes, measured overhead.

Moving the harness into the sandbox is only safe if it changes nothing. This
script proves that the way the report claims it: by re-running the mutants through
the container path and comparing, per mutant, against the verdicts the direct path
already recorded in `docs/results/mutation-<date>.json` — and, separately, by
comparing the *bytes* of the observation stream for the unmutated tree.

Three results are written:

    verdicts     N/N mutants agree with the direct path
    bytes        the observation stream for the case set is byte-identical
    overhead     seconds per pass, direct vs container

Usage:
    ENGINE_SANDBOX_TESTS=1 python scripts/sandbox_gate.py --mutants 300
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

from engine.sandbox import SandboxLimits  # noqa: E402
from harness import determinism  # noqa: E402
from harness.cases import case_set_hash, load_cases  # noqa: E402
from harness.compare import compare_sets  # noqa: E402
from harness.mutate import enumerate_sites, mutate  # noqa: E402
from harness.runner import run_pass  # noqa: E402
from harness.sandbox_transport import DockerExecutor  # noqa: E402


def _client() -> Any:
    import docker

    return docker.from_env()


def _pass(
    *,
    cases: list[Any],
    checkout: Path,
    shim: Path,
    workdir: Path,
    executor: Any | None,
    index: int,
    limits: SandboxLimits,
    image: str,
) -> tuple[Any, float]:
    started = time.monotonic()
    result = run_pass(
        python=sys.executable,
        checkout=checkout,
        shim_dir=shim,
        src_dir=REPO / "src",
        target_paths=[checkout],
        cases=cases,
        index=index,
        workdir=workdir,
        case_budget_seconds=10.0,
        cpu_budget_seconds=10.0,
        expected_case_set_hash=case_set_hash(cases),
        executor=executor,
        sandbox_image=image,
        sandbox_limits=limits,
    )
    return result, time.monotonic() - started


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mutants", type=int, default=300)
    parser.add_argument("--cases", default="harness/cases/parse.json")
    parser.add_argument("--target", default=".harness/target")
    parser.add_argument("--report", default="docs/results/mutation-parse-20261119.json")
    parser.add_argument("--image", default="python:3.12-slim")
    parser.add_argument("--out", default="docs/results/sandbox-gate-20261120.json")
    args = parser.parse_args(argv)

    stored = json.loads((REPO / args.report).read_text(encoding="utf-8"))
    plan, direct_rows = stored["plan"], stored["results"]
    source_path = REPO / args.target / "parse" / "__init__.py"
    sites = {
        site.key: site
        for site in enumerate_sites(source_path.read_text(encoding="utf-8"))
    }
    sample = [sites[key] for key in plan["sample"][: args.mutants]]
    direct_verdicts = {row["site"]: bool(row["harness_killed"]) for row in direct_rows}

    cases = load_cases(REPO / args.cases)
    shim = determinism.write_shim(REPO / ".harness" / "shim")
    work = REPO / ".harness" / "sandbox-gate"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    limits = SandboxLimits(wall_seconds=120.0, mem_mb=512, cpus=2.0, workspace_mb=128)
    client = _client()
    executor = DockerExecutor(client, args.image)

    # 1. bytes: the same case set, direct and containerised, compared as text.
    direct_pass, direct_seconds = _pass(
        cases=cases,
        checkout=(REPO / args.target).resolve(),
        shim=shim,
        workdir=work / "bytes-direct",
        executor=None,
        index=1,
        limits=limits,
        image=args.image,
    )
    sandbox_pass, sandbox_seconds = _pass(
        cases=cases,
        checkout=(REPO / args.target).resolve(),
        shim=shim,
        workdir=work / "bytes-sandbox",
        executor=executor,
        index=1,
        limits=limits,
        image=args.image,
    )
    direct_text = [o.to_json() for o in direct_pass.observations]
    sandbox_text = [o.to_json() for o in sandbox_pass.observations]
    byte_identical = direct_text == sandbox_text
    mismatch = next(
        (
            i
            for i, (a, b) in enumerate(zip(direct_text, sandbox_text, strict=False))
            if a != b
        ),
        None,
    )

    # 2. verdicts: one containerised pass per mutant, compared with the direct run.
    disagree = []
    for position, site in enumerate(sample, start=1):
        mutant_dir = work / "mutant"
        if mutant_dir.exists():
            shutil.rmtree(mutant_dir)
        shutil.copytree(REPO / args.target, mutant_dir)
        source = (mutant_dir / "parse" / "__init__.py").read_text(encoding="utf-8")
        (mutant_dir / "parse" / "__init__.py").write_text(
            mutate(source, site), encoding="utf-8"
        )
        ran, _ = _pass(
            cases=cases,
            checkout=mutant_dir.resolve(),
            shim=shim,
            workdir=work / f"m-{position}",
            executor=executor,
            index=1,
            limits=limits,
            image=args.image,
        )
        verdicts = compare_sets(
            direct_pass.by_case(), ran.by_case()
        )
        killed = any(v.report_class == "different" for v in verdicts.values())
        if killed != direct_verdicts.get(site.key):
            disagree.append(
                {
                    "site": site.key,
                    "direct": direct_verdicts.get(site.key),
                    "sandbox": killed,
                }
            )
        label = "killed" if killed else "survived"
        print(f"[{position}/{len(sample)}] {site.key:28} sandbox={label}", flush=True)

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "image": args.image,
        "mutants": len(sample),
        "verdict_agreement": f"{len(sample) - len(disagree)}/{len(sample)}",
        "disagreements": disagree,
        "byte_identical": byte_identical,
        "first_byte_mismatch_case": (
            None if mismatch is None else direct_pass.observations[mismatch].case_id
        ),
        "case_count": len(cases),
        "control_direct_seconds": round(direct_seconds, 2),
        "control_sandbox_seconds": round(sandbox_seconds, 2),
        "overhead_seconds_per_pass": round(sandbox_seconds - direct_seconds, 2),
        "observations_per_pass": len(cases),
    }
    (REPO / args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    ok = byte_identical and not disagree
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
