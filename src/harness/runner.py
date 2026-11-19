"""Run a case set against a checkout, one controlled interpreter per pass.

A pass is `python -m harness.child plan.json out.jsonl` in a fixed environment
(the determinism shim, PYTHONHASHSEED=0, TZ, locale). Repeats reuse the same
plan and the same environment: that is what makes the A/A baseline meaningful —
anything that differs between two passes of the SAME version is noise, and the
harness must report it rather than let it be blamed on the change under test.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness import determinism
from harness.cases import Case
from harness.observation import Observation


@dataclass(frozen=True)
class Pass:
    """One run of the whole case set."""

    index: int
    observations: tuple[Observation, ...]
    controls: tuple[str, ...]
    stdout: str
    stderr: str

    def by_case(self) -> dict[str, Observation]:
        return {observation.case_id: observation for observation in self.observations}


def run_pass(
    *,
    python: str,
    checkout: Path,
    shim_dir: Path,
    src_dir: Path,
    target_paths: list[Path],
    cases: list[Case],
    index: int,
    module: str = "parse",
    workdir: Path | None = None,
    coverage: bool = False,
) -> Pass:
    workdir = workdir or Path.cwd()
    workdir.mkdir(parents=True, exist_ok=True)
    plan_path = workdir / f"plan-{index}.json"
    out_path = workdir / f"observations-{index}.jsonl"
    plan_path.write_text(
        json.dumps({"module": module, "cases": [case.body() for case in cases]}), encoding="utf-8"
    )
    env = determinism.child_env(checkout, shim_dir, [src_dir, *target_paths])
    if coverage:
        command = [
            python,
            "-m",
            "coverage",
            "run",
            f"--data-file={workdir / 'coverage.data'}",
            f"--source={module}",
            "-m",
            "harness.child",
            str(plan_path),
            str(out_path),
        ]
    else:
        command = [python, "-m", "harness.child", str(plan_path), str(out_path)]
    result = subprocess.run(
        command,
        cwd=str(checkout),
        env=env,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if not out_path.exists():
        raise RuntimeError(
            f"child produced no observations (rc={result.returncode}): "
            f"{result.stderr[-1500:]}"
        )
    observations = tuple(
        _to_observation(json.loads(line))
        for line in out_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    controls = observations[0].controls if observations else ()
    return Pass(
        index=index,
        observations=observations,
        controls=controls,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def _to_observation(body: dict[str, Any]) -> Observation:
    return Observation(
        case_id=body["case_id"],
        entry=body["entry"],
        inputs=body["inputs"],
        outcome=body["outcome"],
        value=body.get("value"),
        exception=body.get("exception"),
        stdout=body.get("stdout", ""),
        stderr=body.get("stderr", ""),
        controls=tuple(body.get("controls") or ()),
    )


def baseline_noise(passes: list[Pass]) -> list[dict[str, Any]]:
    """A/A noise: every case whose canonical observation differs between passes.

    An empty list is the useful result: it means the target is deterministic
    under the shim, so a difference seen later is a behaviour change and not
    scheduling, hashing or the clock.
    """
    if len(passes) < 2:
        return []
    reference = passes[0].by_case()
    noise: dict[str, dict[str, Any]] = {}
    for other in passes[1:]:
        for case_id, observation in other.by_case().items():
            base = reference.get(case_id)
            if base is None:
                noise.setdefault(case_id, {"case_id": case_id, "kind": "missing"})
                continue
            if base.fingerprint != observation.fingerprint:
                kind = _variation_kind(base, observation)
                entry = noise.setdefault(case_id, {"case_id": case_id, "kind": kind, "passes": []})
                entry["passes"].append(other.index)  # type: ignore[union-attr]
        for case_id in reference:
            if case_id not in other.by_case():
                noise.setdefault(case_id, {"case_id": case_id, "kind": "dropped"})
    return sorted(noise.values(), key=lambda item: item["case_id"])


def _variation_kind(left: Observation, right: Observation) -> str:
    if left.outcome != right.outcome:
        return f"outcome:{left.outcome}->{right.outcome}"
    if left.exception != right.exception:
        return "exception"
    if left.stdout != right.stdout or left.stderr != right.stderr:
        return "output"
    return "value"


def digest_map(pass_: Pass) -> dict[str, str]:
    return {observation.case_id: observation.fingerprint for observation in pass_.observations}


def main(argv: list[str] | None = None) -> int:
    """Ad-hoc: `python -m harness.runner <checkout> <cases.json> <passes>`."""
    argv = argv if argv is not None else sys.argv[1:]
    checkout = Path(argv[0]).resolve()
    cases_path = Path(argv[1])
    repeats = int(argv[2]) if len(argv) > 2 else 1
    from harness.cases import load_cases

    cases = load_cases(cases_path)
    shim_dir = determinism.write_shim(Path(".harness-shim").resolve())
    src_dir = Path(__file__).resolve().parents[1]
    passes = [
        run_pass(
            python=sys.executable,
            checkout=checkout,
            shim_dir=shim_dir,
            src_dir=src_dir,
            target_paths=[],
            cases=cases,
            index=index,
        )
        for index in range(1, repeats + 1)
    ]
    print(json.dumps({"passes": repeats, "noise": baseline_noise(passes)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
