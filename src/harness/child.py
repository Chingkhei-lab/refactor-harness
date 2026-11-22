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
from harness.projections import UnknownProjection, canonicalise_stream
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


def _kill_group(pid: int) -> None:
    """POSIX: kill the session's process group (the child started its own)."""
    import signal

    killpg = getattr(os, "killpg", None)
    getpgid = getattr(os, "getpgid", None)
    if killpg is None or getpgid is None:  # Windows typeshed has neither
        return
    killpg(getpgid(pid), getattr(signal, "SIGKILL", 9))


def _kill_process_tree(process: Any) -> None:
    """Kill the child AND its children, on both platforms.

    `subprocess.run(timeout=...)` kills only the direct child, and when the parent
    is itself killed — which is what the pass-wide CPU budget does to a harness
    that overruns — the child is orphaned and keeps spinning. Two such orphans
    each burned 3 500 s of CPU and held their case directories open for an hour,
    which then made those case ids report `unsupported` in every later run. A
    timed-out CLI case has to end its whole tree.
    """
    try:
        if os.name == "nt":
            import subprocess as _subprocess

            _subprocess.run(  # noqa: S603, S607 - taskkill exists on every Windows
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                timeout=30,
            )
        else:
            _kill_group(process.pid)
    except (OSError, ValueError):
        pass
    finally:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=10)
        except Exception:  # noqa: BLE001 - a corpse we cannot reap is not fatal
            pass


_JOB_HANDLE: Any = None


def _assign_to_kill_on_close_job(process: Any) -> None:
    """Windows: tie the child's life to this process (job object, kill on close).

    A case timeout is not the only way a case ends early: when the pass-wide CPU
    budget kills the harness process mid-case, no Python code runs afterwards, so
    the timeout cleanup never happens. Two such orphans each burned 3 500 s of CPU
    and kept their case directories locked. A job object with
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE makes the kernel kill every process in the
    job when the last handle to it closes — which is what happens when the harness
    process dies, however it dies.
    """
    global _JOB_HANDLE
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if _JOB_HANDLE is None:
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                return
            # JOBOBJECT_EXTENDED_LIMIT_INFORMATION = 9, LimitFlags bit 0x2000 = kill on close
            class _Basic(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class _IoCounters(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_uint64),
                    ("WriteOperationCount", ctypes.c_uint64),
                    ("OtherOperationCount", ctypes.c_uint64),
                    ("ReadTransferCount", ctypes.c_uint64),
                    ("WriteTransferCount", ctypes.c_uint64),
                    ("OtherTransferCount", ctypes.c_uint64),
                ]

            class _Extended(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", _Basic),
                    ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            info = _Extended()
            info.BasicLimitInformation.LimitFlags = 0x00002000
            kernel32.SetInformationJobObject(
                wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info)
            )
            _JOB_HANDLE = job
        kernel32.AssignProcessToJobObject(
            wintypes.HANDLE(_JOB_HANDLE), wintypes.HANDLE(int(process._handle))
        )
    except Exception:  # noqa: BLE001 - a job object is an improvement, not a requirement
        return


def _posix_parent_death_signal() -> None:
    """POSIX: SIGKILL this child when the harness process dies (`preexec_fn`)."""
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(1, 9)  # PR_SET_PDEATHSIG, SIGKILL
    except Exception:  # noqa: BLE001 - not every libc exposes prctl here
        return


def _run_cli_process(
    command: list[str], *, cwd: Path, env: dict[str, str], stdin_text: str
) -> tuple[int | None, str, str, int | None]:
    """Run one CLI case, ending the whole process tree on timeout or parent death."""
    import subprocess

    process = subprocess.Popen(  # noqa: S603 - argv list, no shell
        command,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=(os.name != "nt"),
        preexec_fn=None if os.name == "nt" else _posix_parent_death_signal,
    )
    _assign_to_kill_on_close_job(process)
    try:
        out, err = process.communicate(stdin_text.encode(), timeout=_CLI_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _kill_process_tree(process)
        try:
            process.communicate(timeout=10)
        except Exception:  # noqa: BLE001 - pipes may be gone with the tree
            pass
        return None, "", "timeout", None
    exit_code: int | None = process.returncode
    signal_number = None
    if exit_code is not None and exit_code < 0:
        signal_number, exit_code = -exit_code, None
    return exit_code, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), signal_number


def _rmtree_hard(path: Path) -> None:
    """Remove a tree even if a file in it is read-only or briefly locked.

    `ignore_errors=True` is the bug this replaces: it turns "cannot clean up" into
    "the case is unsupported" two lines later.
    """
    import os
    import stat as stat_module

    def clear_readonly(func: Any, target: str, _exc: Any) -> None:
        try:
            os.chmod(target, stat_module.S_IWRITE | stat_module.S_IREAD)
            func(target)
        except OSError:
            pass

    for attempt in range(4):
        if not path.exists():
            return
        shutil.rmtree(path, onerror=clear_readonly)
        if not path.exists():
            return
        time.sleep(0.1 * (attempt + 1))
    if path.exists():
        leftover = next(path.rglob("*"), path)
        raise RuntimeError(
            f"could not clear the case directory {path}: {leftover} is still there. "
            "A leftover case directory is a harness problem, not target behaviour."
        )


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
    # `rmtree(ignore_errors=True)` used to hide a locked or read-only leftover, and
    # the following mkdir then raised FileExistsError — which the outer handler
    # recorded as `unsupported`, i.e. a stale directory on this machine turned a
    # case into an unobservable one, permanently and silently. Now the removal is
    # retried (clearing the read-only bit, which is the Windows failure) and a
    # directory that still survives raises with its path, so the report says
    # "harness could not clear this directory" instead of "the target was opaque".
    _rmtree_hard(cwd)
    cwd.mkdir(parents=True, exist_ok=True)
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
    exit_code, stdout, stderr, signal_number = _run_cli_process(
        command, cwd=cwd, env=env, stdin_text=stdin_text
    )
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
        # Projected, not raw: a traceback names the target's own file, line and
        # source text, which turns every staged copy, every line-shifting edit and
        # every migration into a spurious difference on the two cases that print
        # one. The exception type, its message and the frame names survive.
        "stdout": canonicalise_stream(stdout),
        "stderr": canonicalise_stream(stderr),
        "files": written,
    }
    _rmtree_hard(cwd)
    encoded, _meta = encode_with_meta(value)
    # The projection is named in the observation, not only in the code: a report
    # that compares two values has to be able to say what view of them it
    # compared, and a reader who disagrees with the projection can see that it
    # was on.
    return Observation(
        case_id=case["id"],
        entry=entry,
        inputs=inputs,
        outcome="return",
        value=EncodedValue(encoded),
        controls=(*controls, "cli_stream_projected"),
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


_SCALARS = (str, int, float, bool, type(None))


def _signature(obj: Any) -> str:
    import inspect

    try:
        return str(inspect.signature(obj))
    except (TypeError, ValueError):
        return "<no signature>"


def _scalar(value: Any) -> Any:
    """A value only when its text is stable; otherwise just its type's name.

    `repr()` of an arbitrary object carries an address, which differs in every
    process and would make the snapshot unusable. `str`/`int`/`float`/`bool`/`None`
    reprs are stable and are exactly what a version constant or a default looks
    like.
    """
    if isinstance(value, _SCALARS):
        return {"type": type(value).__name__, "value": repr(value)}
    if isinstance(value, (list, tuple)) and all(isinstance(item, _SCALARS) for item in value):
        return {"type": type(value).__name__, "value": repr(list(value))}
    return {"type": type(value).__name__}


def _surface(obj: Any, *, skip: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Public attribute names of an object, each with signature or stable value."""
    attributes: dict[str, Any] = {}
    for name in sorted(dir(obj)):
        if name.startswith("_") or name in skip:
            continue
        try:
            value = getattr(obj, name)
        except Exception as exc:  # noqa: BLE001 - a property may raise; that is a fact
            attributes[name] = {"error": type(exc).__name__}
            continue
        if callable(value):
            attributes[name] = {"callable": _signature(value)}
        else:
            attributes[name] = _scalar(value)
    return attributes


_PATH_PLACEHOLDERS = (
    ("HARNESS_WORKSPACE", "<workspace>"),
    ("HOME", "<home>"),
    ("XDG_CONFIG_HOME", "<xdg_config_home>"),
    ("TMPDIR", "<tmp>"),
    ("TEMP", "<tmp>"),
    ("TMP", "<tmp>"),
)


def _path_roots() -> list[tuple[str, str]]:
    """The per-run directories whose text must never appear in a snapshot.

    `pycodestyle.USER_CONFIG` is `expanduser('~/.config/pycodestyle')` evaluated at
    import time, so it contains whatever `HOME` the case was given — that is, the
    path of *this* copy of the tree. Two copies of the same tree at two paths then
    differ in a constant nobody changed. The roots are replaced by a fixed marker,
    longest first so `/a/b` wins over `/a`.
    """
    roots: list[tuple[str, str]] = []
    for name, marker in _PATH_PLACEHOLDERS:
        value = os.environ.get(name)
        if value:
            roots.append((value.rstrip("/\\") or value, marker))
    cwd = os.getcwd()
    if cwd:
        roots.append((cwd, "<cwd>"))
    return sorted(roots, key=lambda pair: len(pair[0]), reverse=True)


def _project_paths(value: Any, roots: list[tuple[str, str]]) -> Any:
    """Replace every root by its marker, longest root first.

    The ordering is done here rather than trusted from the caller: a short root that
    is a prefix of a long one (`/a` and `/a/b`) would otherwise half-rewrite the long
    one, which is how a "known" path survives in a snapshot.
    """
    ordered = sorted(roots, key=lambda pair: len(pair[0]), reverse=True)
    return _project_inner(value, ordered)


def _project_inner(value: Any, roots: list[tuple[str, str]]) -> Any:
    if isinstance(value, str):
        for root, marker in roots:
            value = value.replace(root, marker)
        return value
    if isinstance(value, list):
        return [_project_inner(item, roots) for item in value]
    if isinstance(value, dict):
        return {key: _project_inner(item, roots) for key, item in value.items()}
    return value


def _api_snapshot(module: Any, case: dict[str, Any], module_name: str) -> dict[str, Any]:
    names = sorted(
        name for name in dir(module)
        if not name.startswith("_")
    )
    snapshot: dict[str, Any] = {"module": module_name, "names": names,
                               "signatures": {}, "classes": {}, "constants": {}}
    for name in names:
        value = getattr(module, name, None)
        if isinstance(value, type):
            snapshot["classes"][name] = {
                "bases": [base.__name__ for base in value.__bases__],
                "attributes": _surface(value, skip=frozenset({"__dict__"})),
            }
        elif callable(value):
            snapshot["signatures"][name] = _signature(value)
        elif hasattr(value, "__module__"):
            # a module re-exported by the entry module: name only
            reexported = str(getattr(value, "__name__", type(value).__name__))
            snapshot["constants"][name] = {"type": "module", "value": reexported}
        else:
            snapshot["constants"][name] = _scalar(value)
    doc = getattr(module, "__doc__", None)
    snapshot["doc_first_line"] = (doc or "").strip().splitlines()[:1]
    roots = _path_roots()
    snapshot = _project_paths(snapshot, roots)
    call = dict(case.get("call") or {})
    if call:
        entry_point = str(call["entry"])
        target = getattr(module, entry_point)
        produced = target(*call.get("args") or (), **dict(call.get("kwargs") or {}))
        snapshot["call"] = {
            "entry": entry_point,
            "type": type(produced).__name__,
            "bases": [base.__name__ for base in type(produced).__bases__],
            "attributes": _surface(produced),
        }
    return snapshot


def run_api_case(
    module: Any,
    case: dict[str, Any],
    controls: tuple[str, ...],
    case_set_hash: str,
    shim: Any,
) -> Observation:
    """Observe a library's public surface instead of one call's return value.

    A CLI or a single call can only show what somebody invokes. `get_parser()`
    returning an object whose `add_option` disappeared is invisible to both, and
    was the one difference in the stage-2 comparison that the upstream suite saw
    and the harness did not. This kind observes the surface itself: public names,
    signatures, class bases and attributes, and — when the case names a call —
    the surface of the object that call returns.
    """
    module_name = str(case.get("module") or case.get("entry") or "")
    imports = {"module": module_name, "call": dict(case.get("call") or {})}
    _reset(shim)
    snapshot = _api_snapshot(module, case, module_name)
    projected = sorted({marker for _root, marker in _path_roots()})
    encoded, _meta = encode_with_meta(snapshot)
    return Observation(
        case_id=case["id"],
        entry=module_name,
        inputs=imports,
        outcome="return",
        value=EncodedValue(encoded),
        controls=(*controls, "api_surface", *(f"api_paths_projected:{m}" for m in projected)),
        case_set_hash=case_set_hash,
    )


def run_case(
    module: Any,
    case: dict[str, Any],
    controls: tuple[str, ...],
    case_set_hash: str,
    shim: Any,
) -> Observation:
    if case.get("kind") == "cli":
        return run_cli_case(case, controls, case_set_hash, shim)
    if case.get("kind") == "api":
        return run_api_case(module, case, controls, case_set_hash, shim)
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
