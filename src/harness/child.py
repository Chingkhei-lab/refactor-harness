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
import os
import shutil
import sys
import tempfile
import time
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
from harness.projections import UnknownProjection
from harness.projections import get as get_projection


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


_HARD_SECONDS = 3600


def set_hard_cpu_limit(seconds: int) -> None:
    """The pass-wide backstop, supplied by the runner (see runner._hard_cpu_seconds)."""
    global _HARD_SECONDS
    _HARD_SECONDS = max(1, int(seconds))


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
    hard = max(soft + 1, _HARD_SECONDS)
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


# One directory for every CLI case of this pass: it lives inside the pass
# workdir, so in the sandbox it is the tmpfs and never a host path.
_CASE_ROOT = Path(tempfile.gettempdir()) / "harness-cli"
_CLI_TIMEOUT_SECONDS = 60.0

CLI_ENV_ALLOWLIST = (
    "PATH",
    "PYTHONPATH",
    "PYTHONHASHSEED",
    "TZ",
    "LANG",
    "LC_ALL",
    "PYTHONIOENCODING",
    "HARNESS_CWD_FIXED",
    "HARNESS_ENV_SCRUBBED",
    "HARNESS_SHIM_DISABLE",
    # A CLI case is a grandchild of the harness: without these two the target's
    # own code never contributes to the coverage report.
    "COVERAGE_PROCESS_START",
    "COVERAGE_FILE",
)
CLI_COLUMNS = "80"


def _cli_env(home: Path) -> dict[str, str]:
    """The environment a CLI case runs under: an allowlist plus fixed knobs.

    `HOME` and `XDG_CONFIG_HOME` point at an empty directory inside the case's own
    sandbox directory, so a tool that reads user config (pycodestyle reads
    `~/.pycodestyle` and `$XDG_CONFIG_HOME/pycodestyle`) sees nothing. `COLUMNS`
    is fixed because help text wraps by terminal width, and stdin is closed so a
    tool waiting on input fails instead of hanging.
    """
    env = {key: os.environ[key] for key in CLI_ENV_ALLOWLIST if key in os.environ}
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["COLUMNS"] = CLI_COLUMNS
    env.setdefault("LANG", "C.UTF-8")
    env["PYTHONIOENCODING"] = "utf-8"
    env["TMPDIR"] = str(home / "tmp")
    return env


def run_cli_case(
    case: dict[str, Any],
    controls: tuple[str, ...],
    case_set_hash: str,
    shim: Any,
) -> Observation:
    """Run `entry` as a program and observe what a user would see.

    The case gets a fresh working directory with its input files, an empty HOME,
    no TTY, closed stdin, and a fixed COLUMNS. The observation is a value, not an
    exception, so it compares through the ordinary machinery: exit status, stdout,
    stderr, and a manifest of the files the program wrote (relative path, size,
    sha256). A signal death is part of the value too — for a CLI, "killed by
    SIGSEGV" is behaviour, not a harness problem.
    """
    import subprocess

    entry = case["entry"]
    # Resolve before chdir: on the host the child's cwd IS the checkout, so a
    # relative "." is meaningful here and only here.
    program = Path(entry)
    if not program.is_absolute():
        program = Path(case.get("program_root") or ".").resolve() / entry
    argv = [str(item) for item in case.get("argv") or ()]
    inputs = {"argv": argv, "stdin": case.get("stdin") or ""}
    # Deterministic path, not `mkdtemp`: `-vv` prints "local configuration: in
    # <dir>", so a random directory name made a CLI case disagree with itself
    # between two A/A passes. The name is the case id, so the path is identical in
    # every pass on this machine and in both versions of the tree.
    _CASE_ROOT.mkdir(parents=True, exist_ok=True)
    cwd = _CASE_ROOT / str(case["id"])
    shutil.rmtree(cwd, ignore_errors=True)
    cwd.mkdir(parents=True)
    home = cwd / "home"
    (home / ".config").mkdir(parents=True, exist_ok=True)
    (home / "tmp").mkdir(parents=True, exist_ok=True)
    for name, content in (case.get("files") or {}).items():
        target = cwd / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")
    before = _file_manifest(cwd)
    command = [sys.executable, str(program), *argv]
    stdin_text = str(case.get("stdin") or "")
    env = _cli_env(home)
    started = time.monotonic()
    try:
        completed = subprocess.run(  # noqa: S603 - argv list, no shell
            command,
            cwd=str(cwd),
            env=env,
            input=stdin_text.encode(),
            capture_output=True,
            timeout=_CLI_TIMEOUT_SECONDS,
        )
        exit_code: int | None = completed.returncode
        stdout = completed.stdout.decode("utf-8", "replace")
        stderr = completed.stderr.decode("utf-8", "replace")
        signal_number = None
        if exit_code is not None and exit_code < 0:
            signal_number, exit_code = -exit_code, None
    except subprocess.TimeoutExpired:
        exit_code, stdout, stderr, signal_number = None, "", "timeout", None
    after = _file_manifest(cwd)
    written = {
        name: meta for name, meta in after.items() if before.get(name) != meta
    }
    # No timing in the value: a duration is nondeterministic by construction and
    # would need a rule on every CLI case. The pass duration is in the report.
    del started
    value = {
        "exit": exit_code,
        "signal": signal_number,
        "stdout": stdout,
        "stderr": stderr,
        "files": written,
    }
    shutil.rmtree(cwd, ignore_errors=True)
    encoded, _meta = encode_with_meta(value)
    return Observation(
        case_id=case["id"],
        entry=entry,
        inputs=inputs,
        outcome="return",
        value=EncodedValue(encoded),
        controls=controls,
        case_set_hash=case_set_hash,
    )


def _file_manifest(root: Path) -> dict[str, dict[str, Any]]:
    import hashlib

    manifest: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith(("home/", "tmp/")) or rel in ("home", "tmp"):
            continue
        data = path.read_bytes()
        manifest[rel] = {
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    return manifest


def run_case(
    module: Any,
    case: dict[str, Any],
    controls: tuple[str, ...],
    case_set_hash: str,
    shim: Any,
) -> Observation:
    if case.get("kind") == "cli":
        return run_cli_case(case, controls, case_set_hash, shim)
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
        projection_name = case.get("projection") or ""
        with redirect_stdout(out), redirect_stderr(err):
            value = handler(*args, **kwargs)
            if projection_name:
                # A named, allowlisted view: an opaque value becomes observable
                # without the case spec being able to run arbitrary code.
                value = get_projection(projection_name)(value)
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
    # `expect` decides what truncation means. A finite case that hits the item
    # limit is `unsupported (LimitHit)` — the harness did not see the value it
    # was promised. A stream case is *expected* to be cut off, so the bounded
    # prefix is the observation, and two matching prefixes are
    # `equivalent_bounded`, never `identical`.
    finite_limit_hit = flags["truncated"] and case.get("expect", "finite") != "stream"
    unsupported = flags["opaque"] or bool(flags["limit"]) or finite_limit_hit
    if finite_limit_hit:
        flags["limit"] = "items"
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
                    else f"encoder {flags['limit']} limit hit on a finite case; "
                    "value was not fully observed"
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
    # A CLI-only pass has nothing to import: `entry` is a program, not a symbol.
    # Importing an empty module name would turn every case into an import error.
    needs_module = any(case.get("kind") != "cli" for case in plan["cases"])
    try:
        module = (
            importlib.import_module(plan.get("module", "parse"))
            if needs_module
            else None
        )
    except BaseException as exc:  # noqa: BLE001 - a target that cannot import IS a finding
        # A target that fails at import time has changed behaviour just as much
        # as one that crashes inside a call: a mutant that deletes a module-level
        # name is "killed" by every case, not a harness bug. Record one
        # observation per case so the pass completes and the comparison sees it.
        detail = {
            "__import__": {
                "type": type(exc).__name__,
                "message": str(exc)[:400],
                "module": plan.get("module", "parse"),
            }
        }
        with out_path.open("w", encoding="utf-8") as handle:
            for case in plan["cases"]:
                observation = Observation(
                    case_id=case["id"],
                    entry=case["entry"],
                    inputs={"args": case.get("args"), "kwargs": case.get("kwargs")},
                    outcome="exception",
                    exception=detail,
                    case_set_hash=plan["case_set_hash"],
                )
                handle.write(observation.to_json() + chr(10))
        return 0
    case_set_hash = plan["case_set_hash"]
    cpu_budget = float(plan.get("cpu_budget") or 0.0)
    if plan.get("cpu_hard_seconds"):
        set_hard_cpu_limit(int(plan["cpu_hard_seconds"]))
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
            except UnknownProjection as exc:
                observation = Observation(
                    case_id=case["id"],
                    entry=case["entry"],
                    inputs={"args": case.get("args"), "kwargs": case.get("kwargs")},
                    outcome="unsupported",
                    exception={"__exception__": {"type": "UnknownProjection",
                                                 "module": "harness", "message": str(exc)}},
                    controls=controls,
                    case_set_hash=case_set_hash,
                )
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
