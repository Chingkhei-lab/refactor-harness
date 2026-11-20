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


def _stratified(
    sites: dict[str, Any],
    order: list[str],
    verdicts: dict[str, bool],
    size: int,
    seed: int,
) -> list[Any]:
    """A fixed, seeded subset with both verdicts represented.

    The first version took the first N in plan order, which is a fixed subset but
    an arbitrary one: it could be all killed, and then "byte-identical" would only
    have been checked against one kind of mutant. This keeps the killed/survived
    split balanced (at least 10 of each when the size allows), always includes two
    canaries — one known-killed and one known-survived — and is reproducible from
    the seed.
    """
    import random

    rng = random.Random(seed)
    pool = [key for key in order if key in sites]
    killed = [key for key in pool if verdicts.get(key)]
    survived = [key for key in pool if not verdicts.get(key)]
    rng.shuffle(killed)
    rng.shuffle(survived)
    want_killed = max(10, size // 2) if size >= 20 else size // 2
    chosen = killed[: min(len(killed), want_killed)]
    chosen += survived[: max(0, size - len(chosen))]
    if len(chosen) < size:
        chosen += killed[len(chosen) :][: size - len(chosen)]
    # The canaries: one of each verdict, whatever the split produced.
    for key in (killed[0] if killed else None, survived[0] if survived else None):
        if key and key not in chosen:
            chosen.append(key)
    return [sites[key] for key in chosen[:size]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mutants", type=int, default=300)
    parser.add_argument("--cases", default="harness/cases/parse.json")
    parser.add_argument("--target", default=".harness/target")
    parser.add_argument("--report", default="docs/results/mutation-parse-20261119.json")
    parser.add_argument("--image", default="python:3.12-slim")
    parser.add_argument("--out", default="docs/results/sandbox-gate-20261120.json")
    parser.add_argument(
        "--seed",
        type=int,
        default=7,
        help="seed for the killed/survived split, so the CI subset is fixed "
        "rather than 'the first N in plan order'",
    )
    args = parser.parse_args(argv)

    stored = json.loads((REPO / args.report).read_text(encoding="utf-8"))
    plan, direct_rows = stored["plan"], stored["results"]
    source_path = REPO / args.target / "parse" / "__init__.py"
    sites = {
        site.key: site
        for site in enumerate_sites(source_path.read_text(encoding="utf-8"))
    }
    direct_verdicts = {row["site"]: bool(row["harness_killed"]) for row in direct_rows}
    sample = _stratified(sites, plan["sample"], direct_verdicts, args.mutants, args.seed)

    cases = load_cases(REPO / args.cases)
    shim = determinism.write_shim(REPO / ".harness" / "shim")
    work = REPO / ".harness" / "sandbox-gate"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    limits = SandboxLimits(wall_seconds=120.0, mem_mb=512, cpus=2.0, workspace_mb=128)
    from engine.sandbox import DockerSandboxAdapter

    client = DockerSandboxAdapter(_client())
    executor_client = client
    executor = DockerExecutor(client, args.image)

    # 0. where does the per-pass time go? Three probes of the same code path:
    #    P1 no workspace, `true`      -> container create/start/teardown
    #    P2 workspace, `true`         -> adds copy-in + wrapper + copy-out
    #    P3 workspace, the child      -> adds the pass itself
    from engine.sandbox import SandboxSpec
    from engine.sandbox import run as sandbox_run

    probe_limits = SandboxLimits(wall_seconds=120.0, mem_mb=512, cpus=2.0, workspace_mb=128)

    def probe(files: dict[str, bytes], argv: list[str]) -> float:
        started = time.monotonic()
        sandbox_run(
            SandboxSpec(
                image=args.image,
                argv=tuple(argv),
                env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
                workspace_files=files,
                job_id="gate-probe",
            ),
            executor_client,
            probe_limits,
        )
        return round(time.monotonic() - started, 3)

    from harness.sandbox_transport import workspace_files as build_workspace

    workspace = build_workspace(
        checkout=(REPO / args.target).resolve(),
        shim_dir=shim,
        src_dir=REPO / "src",
        plan=b'{"module": "parse", "cases": []}',
    )
    container_only = probe({}, ["/bin/true"])
    copy_round_trip = probe(workspace, ["/bin/true"]) - container_only
    child_only = probe(
        workspace,
        ["python3", "-c", "import harness.child"],
    ) - container_only - copy_round_trip

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
        try:
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
        except Exception as exc:  # noqa: BLE001 - one bad mutant must not hide the rest
            # A gate that dies without a cause is useless: CI logs are admin-only,
            # so the failure has to be printed here to be visible at all. The run
            # continues, and the mutant is reported as an infrastructure failure
            # rather than silently counting as agreement.
            import traceback

            print(f"[{position}/{len(sample)}] {site.key:28} ERROR {type(exc).__name__}")
            traceback.print_exc()
            disagree.append(
                {"site": site.key, "sandbox": "error", "error": f"{type(exc).__name__}: {exc}"}
            )
            continue
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
        "workspace_bytes": sum(len(value) for value in workspace.values()),
        "workspace_files": len(workspace),
        "seed": args.seed,
        "sample_composition": {
            "killed": sum(1 for site in sample if direct_verdicts.get(site.key)),
            "survived": sum(1 for site in sample if not direct_verdicts.get(site.key)),
        },
        "phase_seconds": {
            "container_create_start_teardown": container_only,
            "copy_in_and_copy_out": round(copy_round_trip, 3),
            "child_import": round(child_only, 3),
            "full_pass_overhead": round(sandbox_seconds - direct_seconds, 3),
        },
    }
    (REPO / args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    ok = byte_identical and not disagree
    if not ok:
        print(f"GATE FAILED: byte_identical={byte_identical} "
              f"disagreements={len(disagree)} of {len(sample)}", flush=True)
        for item in disagree[:5]:
            print(f"  disagreement: {item}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
