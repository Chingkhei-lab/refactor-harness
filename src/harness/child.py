"""The child: executes one case set inside a single controlled interpreter.

Run as `python -m harness.child <plan.json> <out.jsonl>`. It imports the target,
runs each case, and writes one canonical observation per line. stdout and stderr
of the *target* are captured around each call, so a target that prints cannot
corrupt the protocol.

Two rules matter here:

- **`reset_controls()` runs before every case**, so no case inherits `random`
  state or the uuid counter from its predecessors. Without it, a case's result
  depends on the ones before it and the A/A baseline cannot see that.
- **The value is encoded with metadata**: an opaque value (no attributes, no
  stable identity), a bounded iterator, or a fired depth/node budget makes the
  case **unsupported**, because comparing it would be asserting an equality the
  harness cannot actually observe.
"""

from __future__ import annotations

import importlib
import io
import json
import sys
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

from harness.observation import (
    EncodedValue,
    Observation,
    encode_exception,
    encode_with_meta,
)


def cpu_seconds() -> float:
    """CPU time this process has used, or 0.0 where `resource` is unavailable."""
    try:
        import resource
    except ImportError:  # Windows: no rlimit, the runner falls back to the clock
        return 0.0
    getrusage = getattr(resource, "getrusage", None)
    which = getattr(resource, "RUSAGE_SELF", None)
    if getrusage is None or which is None:
        return 0.0
    usage = getrusage(which)
    return float(usage.ru_utime + usage.ru_stime)


def install_cpu_budget(budget: float) -> bool:
    """Bound the NEXT case by CPU time, not wall time.

    The soft limit is raised to "CPU used so far + budget" before every case, so
    a case that loops in C (a regex backtracking, a tight numeric loop) is killed
    by SIGXCPU at its budget even though it never returns to the interpreter —
    which a wall-clock timeout can only do by killing the process from outside,
    indistinguishable from a slow machine.

    The hard limit is a pass-wide backstop and is set once: POSIX allows lowering
    a hard limit but not raising it again, so a per-case hard limit would make
    the second case unmeasurable. Soft alone terminates the case: SIGXCPU's
    default action is terminate.
    """
    try:
        import resource
    except ImportError:
        return False
    limit = getattr(resource, "RLIMIT_CPU", None)
    setrlimit = getattr(resource, "setrlimit", None)
    if limit is None or setrlimit is None:
        return False
    used = int(cpu_seconds())
    soft = used + max(1, int(round(budget)))
    hard = max(soft + 1, 3600)
    try:
        setrlimit(limit, (soft, hard))
    except (ValueError, OSError):
        return False
    return True


def _shim() -> Any:
    try:
        return importlib.import_module("sitecustomize")
    except Exception:
        return None


def _controls(shim: Any) -> tuple[str, ...]:
    return tuple(getattr(shim, "CONTROLS", ()))


def _reset(shim: Any) -> None:
    reset = getattr(shim, "reset_controls", None)
    if callable(reset):
        reset()


def run_case(
    module: Any,
    case: dict[str, Any],
    controls: tuple[str, ...],
    case_set_hash: str,
    shim: Any,
) -> Observation:
    entry = case["entry"]
    args = tuple(case.get("args") or ())
    kwargs = dict(case.get("kwargs") or {})
    inputs = {"args": list(args), "kwargs": kwargs}
    _reset(shim)
    try:
        handler = getattr(module, entry)
    except AttributeError as exc:
        return Observation(
            case_id=case["id"],
            entry=entry,
            inputs=inputs,
            outcome="unsupported",
            exception=encode_exception(exc),
            controls=controls,
            case_set_hash=case_set_hash,
        )

    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            value = handler(*args, **kwargs)
    except BaseException as exc:  # noqa: BLE001 - an exception IS the observation
        return Observation(
            case_id=case["id"],
            entry=entry,
            inputs=inputs,
            outcome="exception",
            exception=encode_exception(exc),
            stdout=out.getvalue(),
            stderr=err.getvalue(),
            controls=controls,
            case_set_hash=case_set_hash,
        )

    # encode inside the child: a lazy object can only be materialised here
    encoded, meta = encode_with_meta(value)
    flags = {
        "opaque": bool(meta["opaque"]),
        "truncated": bool(meta["truncated"]),
        "limit": meta["limit"],
        "nodes": int(meta["nodes"]),
    }
    unsupported = flags["opaque"] or bool(flags["limit"])
    return Observation(
        case_id=case["id"],
        entry=entry,
        inputs=inputs,
        outcome="unsupported" if unsupported else "return",
        value=EncodedValue(encoded),
        exception=(
            {
                "__exception__": {
                    "type": "UnobservableValue",
                    "module": "harness",
                    "message": (
                        "value has no stable identity (opaque) — comparing it "
                        "would assert an equality the harness cannot observe"
                    )
                    if flags["opaque"]
                    else f"encoder {flags['limit']} budget fired; value was not fully encoded"
                }
            }
            if unsupported
            else None
        ),
        stdout=out.getvalue(),
        stderr=err.getvalue(),
        controls=controls,
        flags=flags,
        case_set_hash=case_set_hash,
    )


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    plan_path, out_path = Path(argv[0]), Path(argv[1])
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    module = importlib.import_module(plan.get("module", "parse"))
    case_set_hash = plan["case_set_hash"]
    cpu_budget = float(plan.get("cpu_budget") or 0.0)
    shim = _shim()
    controls = _controls(shim)
    # Begin markers: if the process dies inside a case (a CPU budget kill), the
    # runner needs to know WHICH case was in flight, and the last written
    # observation does not say that.
    begin_path = out_path.with_suffix(".begin")
    with out_path.open("w", encoding="utf-8") as handle, begin_path.open(
        "w", encoding="utf-8"
    ) as begun:
        for case in plan["cases"]:
            begun.write(case["id"] + chr(10))
            begun.flush()
            # No rlimit on this platform (Windows): the runner's wall clock is
            # then the only bound, and the report says so.
            install_cpu_budget(cpu_budget) if cpu_budget > 0 else None
            try:
                observation = run_case(module, case, controls, case_set_hash, shim)
            except BaseException:  # noqa: BLE001 - harness bug, record it, keep going
                observation = Observation(
                    case_id=case["id"],
                    entry=case["entry"],
                    inputs={"args": case.get("args"), "kwargs": case.get("kwargs")},
                    outcome="unsupported",
                    exception={"__exception__": {"type": "HarnessError", "module": "harness",
                                                 "message": traceback.format_exc()[-800:]}},
                    controls=controls,
                    case_set_hash=case_set_hash,
                )
            handle.write(observation.to_json() + "\n")
            handle.flush()  # a killed child must still leave its progress behind
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
