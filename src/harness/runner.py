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
import time
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
    restarts: int = 0

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
    executor: Any | None = None,
    sandbox_image: str = "python:3.12-slim",
    sandbox_limits: Any | None = None,
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
    # Absolute: the child runs with `cwd=checkout`, so a relative workdir would
    # make it look for the plan inside the target tree.
    workdir = (workdir or Path.cwd()).resolve()
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
    restarts = 0
    for _attempt in range(attempts):
        plan_path.write_text(
            json.dumps(
                {
                    "module": module,
                    "case_set_hash": set_hash,
                    "cpu_budget": cpu_budget_seconds,
                    "cpu_hard_seconds": _hard_cpu_seconds(cpu_budget_seconds, len(remaining)),
                    "cases": [case.body() for case in remaining],
                }
            ),
            encoding="utf-8",
        )
        # A per-case wall watchdog, not a pass-wide timeout. The pass-wide form
        # was `case_budget * len(remaining)`, so on a platform without RLIMIT_CPU
        # one hanging case gave the child hours: a mutant that loops forever
        # stalled the whole experiment (observed at mutant 111 of 300). The child
        # writes a begin marker and an observation per case, so "no progress for
        # `case_budget_seconds`" identifies a hung case in bounded time and lets
        # the pass resume after it.
        wall_timeout = (
            (cpu_budget_seconds + case_budget_seconds + 5.0) * max(1, len(remaining))
            if cpu_budget_seconds > 0
            else case_budget_seconds * max(1, len(remaining))
        )
        try:
            if executor is None:
                timed_out, returncode, last_stderr = _supervise(
                    command,
                    cwd=checkout,
                    env=env,
                    out_path=out_path,
                    begin_path=begin_path,
                    stall_seconds=case_budget_seconds,
                    wall_timeout=wall_timeout,
                )
            else:
                timed_out, returncode, last_stderr = _supervise_sandbox(
                    executor=executor,
                    plan_path=plan_path,
                    out_path=out_path,
                    begin_path=begin_path,
                    checkout=checkout,
                    shim_dir=shim_dir,
                    src_dir=src_dir,
                    env=env,
                    limits=sandbox_limits,
                    image=sandbox_image,
                    index=index,
                    wall_seconds=wall_timeout,
                )
        except OSError as exc:
            raise RuntimeError(f"could not start the child: {exc}") from exc
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
            # The child died inside a case. Which case, and why, decides whether
            # this is a finding or a harness bug:
            #
            #   no begin marker       -> the harness itself broke; raise
            #   SIGXCPU               -> the case outlived its CPU budget
            #   SIGKILL               -> a resource kill (OOM, or the pass-wide
            #                            hard backstop): a property of the limits,
            #                            not of the target
            #   any other exit/signal -> THE TARGET CRASHED. A refactor that
            #                            introduces an abort, a segfault or an
            #                            os._exit changed behaviour, and calling
            #                            that a harness bug would hide exactly the
            #                            defect this exists to find.
            in_flight = _in_flight(begin_path)
            if in_flight is None:
                raise RuntimeError(
                    f"child exited {returncode} before beginning any case; "
                    f"stderr: {last_stderr[-800:]}"
                )
            stuck = next((case for case in missing if case.id == in_flight), None)
            if stuck is None:
                raise RuntimeError(
                    f"begin marker {in_flight!r} is not a pending case; stderr: "
                    f"{last_stderr[-800:]}"
                )
            restarts += 1
            remaining = [case for case in missing if case.id != stuck.id]
            if returncode is None:
                raise RuntimeError(
                    f"child exited without a status inside case {in_flight!r}"
                )
            done[stuck.id] = _death_observation(stuck, returncode, last_stderr)
            continue
        stuck, remaining = missing[0], missing[1:]
        done[stuck.id] = Observation(
            case_id=stuck.id,
            entry=stuck.entry,
            inputs={"args": list(stuck.args), "kwargs": dict(stuck.kwargs)},
            outcome="unsupported",
            exception={
                "__exception__": {
                    "type": "CaseTimeout",
                    "module": "harness",
                    "message": (
                        "no result within the wall-clock backstop; recorded as "
                        "unsupported (a budget, not a comparison)"
                    ),
                }
            },
        )
    # A pass that ran out of attempts still reports every case: one that was
    # never reached is `unsupported (NotRun)`, not a silent gap in the counts.
    executed = {item.id for item in execution}
    for case in cases:
        if case.id in executed and case.id not in done:
            done[case.id] = Observation(
                case_id=case.id,
                entry=case.entry,
                inputs={"args": list(case.args), "kwargs": dict(case.kwargs)},
                outcome="unsupported",
                exception={
                    "__exception__": {
                        "type": "NotRun",
                        "module": "harness",
                        "message": (
                            f"{attempts} child restarts were used before this case; "
                            "recorded as unsupported (not run), not as equal"
                        ),
                    }
                },
            )
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
        restarts=restarts,
    )


def _supervise_sandbox(
    *,
    executor: Any,
    plan_path: Path,
    out_path: Path,
    begin_path: Path,
    checkout: Path,
    shim_dir: Path,
    src_dir: Path,
    env: dict[str, str],
    limits: Any,
    image: str,
    index: int,
    wall_seconds: float,
) -> tuple[bool, int | None, str]:
    """One attempt with the child inside the sandbox; same return contract.

    The workspace travels as copy-in bytes and the observations come back from
    the copy-out, so a child killed by its CPU budget still leaves both its
    observations and its begin marker behind: the sandbox supervisor runs the
    payload as a child and keeps the container alive for the copy-out.
    """
    from engine.sandbox import SandboxLimits
    from harness.sandbox_transport import (
        OUT_NAME,
        PLAN_NAME,
        sandbox_env,
        workspace_files,
    )

    limits = limits or SandboxLimits(wall_seconds=max(30.0, wall_seconds))

    files = workspace_files(
        checkout=checkout,
        shim_dir=shim_dir,
        src_dir=src_dir,
        plan=plan_path.read_bytes(),
    )
    env = sandbox_env(env)
    argv = [
        str(getattr(executor, "python", "python3")),
        "-m",
        "harness.child",
        PLAN_NAME,
        OUT_NAME,
    ]
    started = time.monotonic()
    outcome = executor.run(
        argv=argv,
        env=env,
        files=files,
        limits=limits,
        image=image,
        job_id=f"harness-{index}",
    )
    del started, wall_seconds  # the sandbox runner owns the wall clock
    # Copy the results back to the host paths the retry loop already reads.
    begin_name = OUT_NAME.replace(".jsonl", ".begin")
    for name, path in ((OUT_NAME, out_path), (begin_name, begin_path)):
        data = outcome.files.get(name)
        if data is not None:
            path.write_bytes(data)
    stderr = outcome.stderr.decode("utf-8", "replace") if outcome.stderr else ""
    if outcome.oom_killed:
        stderr = (stderr + chr(10) + "sandbox: container was OOM-killed").strip()
    return outcome.timed_out, outcome.exit_code, stderr[-2000:]


def _line_count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("rb") as handle:
        return sum(1 for _ in handle)


def _supervise(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    out_path: Path,
    begin_path: Path,
    stall_seconds: float,
    wall_timeout: float,
) -> tuple[bool, int | None, str]:
    """Run the child, killing it after `stall_seconds` with no new case.

    Returns `(timed_out, returncode, stderr_tail)`. A stall kill leaves the last
    observation written, so the caller can name the case that was in flight.
    """
    import time

    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    started = time.monotonic()
    last_progress = started
    progress = _line_count(out_path) + _line_count(begin_path)
    timed_out = False
    while process.poll() is None:
        time.sleep(0.05)
        current = _line_count(out_path) + _line_count(begin_path)
        if current != progress:
            progress = current
            last_progress = time.monotonic()
            continue
        now = time.monotonic()
        if now - last_progress > stall_seconds or now - started > wall_timeout:
            timed_out = True
            process.kill()
            break
    _, stderr = process.communicate()
    return timed_out, process.returncode, (stderr or "")[-2000:]


def _in_flight(begin_path: Path) -> str | None:
    """The case the child had begun when it died, from its begin markers."""
    if not begin_path.exists():
        return None
    lines = [line.strip() for line in begin_path.read_text(encoding="utf-8").splitlines()]
    return lines[-1] if lines else None


def _hard_cpu_seconds(per_case: float, cases: int) -> int:
    """The pass-wide hard CPU limit, derived rather than guessed.

    POSIX forbids raising a hard limit once it is lowered, so the hard limit is
    set once per pass and must cover the legitimate total CPU of the whole pass,
    or it would kill a healthy late case. Each case is allowed `per_case` CPU
    seconds (its soft limit), so a pass cannot legitimately exceed
    `per_case * cases`; the hard limit is that doubled, with a 60 s floor for a
    short pass.

    With the ordinary settings (10 s per case, 273 cases) the hard limit is
    5 460 s. A full pass of the `parse` case set measures well under 60 s of CPU,
    so the headroom is ~90x: the hard limit exists to bound a process that ignores
    SIGXCPU, not to be the case budget.
    """
    if per_case <= 0:
        return 3600
    return max(60, int(2 * per_case * max(1, cases)))


# The POSIX numbers, so a signal is classified the same on a host that lacks the
# constant (Windows has no SIGXCPU, but the sandbox reports 128+24 for it).
_SIGXCPU = getattr(signal, "SIGXCPU", 24)
_SIGKILL = getattr(signal, "SIGKILL", 9)


def _signal_name(number: int) -> str:
    if number == _SIGXCPU:
        return "SIGXCPU"
    if number == _SIGKILL:
        return "SIGKILL"
    try:
        return signal.Signals(number).name
    except ValueError:
        return f"signal {number}"


def _death_observation(case: Case, returncode: int, stderr: str) -> Observation:
    """Classify how the child died: budget, resource, or the target crashing.

    Only the first two are `unsupported`. A crash is a behaviour, recorded with
    its exit status or signal so two versions can be compared on it: "old
    returned, new aborted" is a difference, not an infrastructure problem.

    Two conventions reach here. A host subprocess reports a signal as a negative
    return code; the sandbox supervisor is a shell, so it reports 128+n. Both must
    classify the same way, or a CPU kill inside the container would be compared as
    "the target exited 152" instead of recorded as a budget.
    """
    signal_number = None
    multiple_of_128 = False
    if returncode is not None and returncode < 0:
        signal_number = -returncode
    elif returncode is not None and 128 < returncode < 160:
        candidate = returncode - 128
        # A shell's 128+n is only a signal if n names one; otherwise it is just an
        # exit status that happens to be large.
        if candidate in (_SIGXCPU, _SIGKILL) or _signal_name(candidate).startswith("SIG"):
            signal_number, multiple_of_128 = candidate, True
    if signal_number is None:
        name, kind, outcome = "", "ProcessExit", "exception"
    else:
        name = _signal_name(signal_number)
        if name == "SIGXCPU":
            kind, outcome = "CpuBudget", "unsupported"
        elif name == "SIGKILL":
            # OOM killer or the pass-wide hard backstop: a property of the
            # limits, not of the target's behaviour.
            kind, outcome = "ResourceKill", "unsupported"
        else:
            kind, outcome = "ProcessSignal", "exception"
    exit_code = None
    if returncode is not None and signal_number is None:
        exit_code = returncode
    return Observation(
        case_id=case.id,
        entry=case.entry,
        inputs={"args": list(case.args), "kwargs": dict(case.kwargs)},
        outcome=outcome,
        exception={
            "__process__": {
                "kind": kind,
                # Keep the shell-convention code visible: a report reader can tell
                # "exited 152" from "killed by signal 24" without guessing.
                "exit_code": exit_code,
                "shell_exit_code": returncode if multiple_of_128 else None,
                "signal": signal_number,
                "signal_name": name,
                "stderr_tail": stderr[-400:],
            }
        },
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
