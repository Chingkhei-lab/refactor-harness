"""Run a case set against a checkout, one controlled interpreter per pass.

A pass is `python -m harness.child plan.json out.jsonl` in a fixed environment
(the determinism shim, PYTHONHASHSEED=0, TZ, locale). Repeats reuse the same
plan and the same environment: that is what makes the A/A baseline meaningful —
anything that differs between two passes of the SAME version is noise, and the
harness must report it rather than let it be blamed on the change under test.
"""

from __future__ import annotations

import json
import random
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness import determinism
from harness.cases import Case
from harness.observation import EncodedValue, Observation


@dataclass(frozen=True)
class Pass:
    """One run of the whole case set."""

    index: int
    observations: tuple[Observation, ...]
    controls: tuple[str, ...]
    stdout: str
    stderr: str
    case_set_hash: str = ""
    order: str = "fixed"

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
    case_budget_seconds: float = 20.0,
    attempts: int = 3,
    order: str = "fixed",
    shuffle_seed: int = 0,
    expected_case_set_hash: str = "",
    shim_disable: tuple[str, ...] = (),
    cpu_budget_seconds: float = 0.0,
) -> Pass:
    from harness.cases import case_set_hash as compute_case_set_hash

    set_hash = compute_case_set_hash(list(cases))
    if expected_case_set_hash and expected_case_set_hash != set_hash:
        raise RuntimeError(
            "case set changed between the golden run and this pass: "
            f"expected {expected_case_set_hash[:12]}, computed {set_hash[:12]}. "
            "Refusing to compare observations from different case sets."
        )
    execution = list(cases)
    if order == "shuffled":
        # Same cases, different order per pass: if any case's result depends on
        # what earlier cases consumed, this is what exposes it.
        random.Random(shuffle_seed + index).shuffle(execution)
    workdir = workdir or Path.cwd()
    workdir.mkdir(parents=True, exist_ok=True)
    plan_path = workdir / f"plan-{index}.json"
    out_path = workdir / f"observations-{index}.jsonl"
    begin_path = out_path.with_suffix(".begin")
    plan_path.write_text(
        json.dumps({"module": module, "cases": [case.body() for case in cases]}), encoding="utf-8"
    )
    env = determinism.child_env(
        checkout, shim_dir, [src_dir, *target_paths], shim_disable=shim_disable
    )
    if coverage:
        command = [
            python,
            "-m",
            "coverage",
            "run",
            f"--data-file={workdir / 'coverage.data'}",
            "--branch",
            f"--source={module}",
            "-m",
            "harness.child",
            str(plan_path),
            str(out_path),
        ]
    else:
        command = [python, "-m", "harness.child", str(plan_path), str(out_path)]
    # One case can hang forever (a target infinite loop is not a harness bug, but
    # it must not eat the pass). The child flushes each observation, so on a
    # timeout the harness can see exactly where it stopped, record that case as
    # unsupported, and resume from the next one.
    done: dict[str, Observation] = {}
    remaining = list(execution)
    last_stderr = ""
    for _ in range(attempts):
        plan_path.write_text(
            json.dumps(
                {
                    "module": module,
                    "case_set_hash": set_hash,
                    "cpu_budget": cpu_budget_seconds,
                    "cases": [case.body() for case in remaining],
                }
            ),
            encoding="utf-8",
        )
        wall_timeout = (
            (cpu_budget_seconds + 5.0) * max(1, len(remaining))
            if cpu_budget_seconds > 0
            else case_budget_seconds * max(1, len(remaining))
        )
        try:
            result = subprocess.run(
                command,
                cwd=str(checkout),
                env=env,
                capture_output=True,
                text=True,
                timeout=wall_timeout,
            )
            timed_out = False
            returncode = result.returncode
            last_stderr = result.stderr or ""
        except subprocess.TimeoutExpired as exc:
            last_stderr = str(exc)
            timed_out = True
            returncode = None
        if out_path.exists():
            for line in out_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    observation = _to_observation(json.loads(line))
                    done[observation.case_id] = observation
            out_path.unlink()
        missing = [case for case in remaining if case.id not in done]
        if not missing:
            break
        if not timed_out:
            # The child died inside a case. The begin marker says which one: if
            # it was killed by its CPU budget (SIGXCPU/SIGKILL) that case is
            # `unsupported (CpuBudget)`, and the rest of the set resumes in a
            # fresh child. Any other death is a harness bug and must not be
            # papered over as a target finding.
            in_flight = _in_flight(begin_path)
            killed = _death_by_budget(returncode)
            if in_flight is None or not killed:
                raise RuntimeError(
                    f"child exited {returncode} with {len(missing)} case(s) left; "
                    f"last begun: {in_flight}; stderr: {last_stderr[-800:]}"
                )
            stuck = next((case for case in missing if case.id == in_flight), None)
            if stuck is None:
                raise RuntimeError(
                    f"begin marker {in_flight!r} is not a pending case; stderr: "
                    f"{last_stderr[-800:]}"
                )
            remaining = [case for case in missing if case.id != stuck.id]
            done[stuck.id] = _budget_observation(stuck, cpu_budget_seconds)
            continue
        stuck, remaining = missing[0], missing[1:]
        done[stuck.id] = _budget_observation(stuck, case_budget_seconds, wall=True)
    observations = tuple(done[case.id] for case in cases if case.id in done)
    controls = observations[0].controls if observations else ()
    return Pass(
        index=index,
        observations=observations,
        controls=controls,
        stdout="",
        stderr=last_stderr[-2000:],
        case_set_hash=set_hash,
        order=order,
    )


def _in_flight(begin_path: Path) -> str | None:
    """The case the child had begun when it died, from its begin markers."""
    if not begin_path.exists():
        return None
    lines = [line.strip() for line in begin_path.read_text(encoding="utf-8").splitlines()]
    return lines[-1] if lines else None


def _death_by_budget(returncode: int | None) -> bool:
    """True when the child was killed by a CPU-budget signal.

    SIGXCPU (24) is the soft limit; SIGKILL (9) is the hard backstop. Both mean
    the case outlived its budget rather than the harness breaking: the case is
    `unsupported (CpuBudget)`, and that is a measurement about the case, not a
    claim that its behaviour is equal.
    """
    if returncode is None:
        return False
    budget_signals = {
        getattr(signal, name)
        for name in ("SIGXCPU", "SIGKILL")
        if hasattr(signal, name)
    }
    return returncode < 0 and -returncode in budget_signals


def _budget_observation(
    case: Case, budget: float, *, wall: bool = False
) -> Observation:
    kind = "CaseTimeout" if wall else "CpuBudget"
    message = (
        f"no result within {budget:.0f}s of wall clock; recorded as unsupported "
        "(a target hang, not a comparison)"
        if wall
        else f"exceeded {budget:.0f}s of CPU; recorded as unsupported (a budget, "
        "not a comparison)"
    )
    return Observation(
        case_id=case.id,
        entry=case.entry,
        inputs={"args": list(case.args), "kwargs": dict(case.kwargs)},
        outcome="unsupported",
        exception={"__exception__": {"type": kind, "module": "harness", "message": message}},
    )


def _to_observation(body: dict[str, Any]) -> Observation:
    return Observation(
        case_id=body["case_id"],
        entry=body["entry"],
        inputs=body["inputs"],
        outcome=body["outcome"],
        value=EncodedValue(body.get("value")),
        exception=body.get("exception"),
        stdout=body.get("stdout", ""),
        stderr=body.get("stderr", ""),
        controls=tuple(body.get("controls") or ()),
        flags=dict(body.get("flags") or {}),
        case_set_hash=body.get("case_set_hash", ""),
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
                entry["passes"].append(other.index)
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
