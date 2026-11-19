"""The child: executes one case set inside a single controlled interpreter.

Run as `python -m harness.child <plan.json> <out.jsonl>`. It imports the target,
runs each case, and writes one canonical observation per line. stdout and stderr
of the *target* are captured around each call, so a target that prints cannot
corrupt the protocol.

Nothing here decides whether a result is right — it records what happened,
including the exception type and message.
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

from harness.observation import Observation, encode, encode_exception


def _controls() -> tuple[str, ...]:
    try:
        sitecustomize = importlib.import_module("sitecustomize")
    except Exception:
        return ()
    return tuple(getattr(sitecustomize, "CONTROLS", ()))


def run_case(module: Any, case: dict[str, Any], controls: tuple[str, ...]) -> Observation:
    entry = case["entry"]
    args = tuple(case.get("args") or ())
    kwargs = dict(case.get("kwargs") or {})
    out, err = io.StringIO(), io.StringIO()
    inputs = {"args": list(args), "kwargs": kwargs}
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
        )

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
        )
    # encode inside the child: a lazy object can only be materialised here
    return Observation(
        case_id=case["id"],
        entry=entry,
        inputs=inputs,
        outcome="return",
        value=encode(value),
        stdout=out.getvalue(),
        stderr=err.getvalue(),
        controls=controls,
    )


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    plan_path, out_path = Path(argv[0]), Path(argv[1])
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    module = importlib.import_module(plan.get("module", "parse"))
    controls = _controls()
    with out_path.open("w", encoding="utf-8") as handle:
        for case in plan["cases"]:
            try:
                observation = run_case(module, case, controls)
            except BaseException:  # noqa: BLE001 - harness bug, record it, keep going
                observation = Observation(
                    case_id=case["id"],
                    entry=case["entry"],
                    inputs={"args": case.get("args"), "kwargs": case.get("kwargs")},
                    outcome="unsupported",
                    exception={"__exception__": {"type": "HarnessError", "module": "harness",
                                                 "message": traceback.format_exc()[-800:]}},
                    controls=controls,
                )
            handle.write(observation.to_json() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
