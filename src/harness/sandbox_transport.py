"""Run a harness pass inside the Phase 3 sandbox instead of on the host.

ADR 003 D4 is explicit: **no host mounts**. The workspace is copied in as a tar
stream into a size-capped tmpfs, the process runs inside the container, and the
results are copied out as a tar stream. This module keeps that contract: it
builds the whole workspace (checkout, shim, harness sources, plan) as bytes and
asks the sandbox runner to copy it in.

Two things make the harness a good fit for the existing runner:

- the child already writes each observation and its begin marker with a flush, so
  the observations are on disk inside the tmpfs when the child dies;
- the runner's supervisor wrapper runs the payload as a **child** and then prints
  its exit code on the nonced marker line, so a payload killed by SIGXCPU or
  SIGKILL does not take the container with it — the wrapper survives, the marker
  arrives, and the copy-out still happens. That is how observations come out of a
  dead payload; a wall-clock kill of the container itself loses them by design
  (the tmpfs dies), which is why the per-case budget is an RLIMIT_CPU inside.

The executor is a parameter so the transport can be tested without a daemon:
`DockerExecutor` for real runs, `LocalExecutor` for tests (same copy-in/copy-out
shape, a temp directory instead of a tmpfs).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from engine.sandbox import DockerSandboxAdapter, SandboxLimits, SandboxSpec
from engine.sandbox import run as sandbox_run

WORKSPACE = "/workspace"
# The container is always Linux, so its PATH is fixed here and never inherited.
SANDBOX_PATH = "/usr/local/bin:/usr/bin:/bin"
PLAN_NAME = "plan.json"
OUT_NAME = "out.jsonl"


@dataclass(frozen=True)
class SandboxOutcome:
    """What one sandboxed pass attempt produced."""

    exit_code: int | None
    timed_out: bool
    stderr: bytes
    files: dict[str, bytes]
    container_id: str | None = None
    duration: float = 0.0
    oom_killed: bool = False
    # cgroup evidence for a 137 exit: None means "not readable", not "no OOM".
    oom_counter: int | None = None
    limits_note: str = ""


class Executor(Protocol):
    """The seam between the harness and whoever actually runs the child."""

    def run(
        self,
        *,
        argv: list[str],
        env: dict[str, str],
        files: dict[str, bytes],
        limits: SandboxLimits,
        image: str,
        job_id: str,
    ) -> SandboxOutcome: ...


def _tree(root: Path, prefix: str, skip: tuple[str, ...] = ()) -> dict[str, bytes]:
    """Collect a directory tree as workspace-relative bytes for copy-in."""
    out: dict[str, bytes] = {}
    root = root.resolve()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if any(part in skip for part in Path(rel).parts):
            continue
        if "__pycache__" in rel or rel.endswith(".pyc"):
            continue
        out[f"{prefix}/{rel}".lstrip("/")] = path.read_bytes()
    return out


def workspace_files(
    *,
    checkout: Path,
    shim_dir: Path,
    src_dir: Path,
    plan: bytes,
    extra: dict[str, bytes] | None = None,
) -> dict[str, bytes]:
    """Everything the child needs, as copy-in bytes: no host path travels.

    Layout inside the tmpfs:

        /workspace/plan.json      the pass plan
        /workspace/src/harness/*  the child itself
        /workspace/shim/*         sitecustomize
        /workspace/checkout/*     the target tree under test
    """
    files: dict[str, bytes] = {}
    # Only the harness package is copied in. The engine, migrations and SQL are
    # not imported by `harness.child`, so sending them is pure copy-in cost on
    # every mutant — measured as part of the gate's phase breakdown.
    files.update(_tree(src_dir / "harness", "src/harness", skip=("__pycache__",)))
    files.update(_tree(shim_dir, "shim"))
    files.update(_tree(checkout, "checkout"))
    files[PLAN_NAME] = plan
    if extra:
        files.update(extra)
    return files


def sandbox_env(base_env: dict[str, str]) -> dict[str, str]:
    """The child's environment, with import paths rewritten for the tmpfs.

    Same allowlist as the host path (the controls live in these variables), but
    nothing may point at a host path: inside the container there is no `/c/...`
    and no checkout outside `/workspace`.
    """
    env = {
        key: value
        for key, value in base_env.items()
        if key
        in {
            "HARNESS_PYTHON",
            "HARNESS_CWD_FIXED",
            "HARNESS_ENV_SCRUBBED",
            "PYTHONHASHSEED",
            "TZ",
            "LANG",
            "LC_ALL",
            "HARNESS_SHIM_DISABLE",
            "PYTHONIOENCODING",
            "COVERAGE_PROCESS_START",
            "COVERAGE_FILE",
        }
    }
    # Override, never default: on a Windows host the base PATH is
    # `C:\Windows\...`, and handing that to a Linux container makes the
    # wrapper's own `sleep` unfindable (measured: the container logged
    # "sleep: not found" and the copy-in tar exited 127).
    env["PATH"] = SANDBOX_PATH
    env["PYTHONPATH"] = f"{WORKSPACE}/shim:{WORKSPACE}/src:{WORKSPACE}/checkout"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["HOME"] = "/tmp"
    return env


class DockerExecutor:
    """The real thing: copy-in tar -> tmpfs, run, copy-out tar."""

    # The interpreter *inside* the image: never a host path.
    python = "python3"

    def __init__(self, client: Any, image: str) -> None:
        # `sandbox.run` speaks the runner's own small protocol, not the SDK:
        # passing the raw docker client fails at the first `client.create(...)`,
        # which is exactly what a red CI run taught this transport.
        self.client = (
            client if hasattr(client, "create") else DockerSandboxAdapter(client)
        )
        self.image = image

    def run(
        self,
        *,
        argv: list[str],
        env: dict[str, str],
        files: dict[str, bytes],
        limits: SandboxLimits,
        image: str,
        job_id: str,
    ) -> SandboxOutcome:
        spec = SandboxSpec(
            image=image or self.image,
            argv=tuple(argv),
            env=env,
            workspace_files=files,
            job_id=job_id,
        )
        started = time.monotonic()
        result = sandbox_run(spec, self.client, limits)
        return SandboxOutcome(
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            stderr=result.stderr,
            files={
                name: data
                for name, data in result.files.items()
                if not name.startswith(".engine_")
            },
            container_id=result.container_id,
            duration=round(time.monotonic() - started, 3),
            oom_killed=result.oom_killed,
            oom_counter=result.oom_counter,
        )


class LocalExecutor:
    """Same copy-in/copy-out shape without a daemon — for tests and dry runs.

    It is NOT the security boundary: it runs the child as the invoking user on
    the host. It exists so the transport's bookkeeping (workspace layout, argv,
    copy-out, death classification) can be tested on a machine with no Docker.
    """

    # The interpreter that exists on the host running the test.
    python = sys.executable

    def __init__(self, work_root: Path | None = None) -> None:
        self.root = (
            Path(tempfile.mkdtemp(prefix="harness-local-"))
            if work_root is None
            else work_root
        )
        self.root.mkdir(parents=True, exist_ok=True)

    def run(
        self,
        *,
        argv: list[str],
        env: dict[str, str],
        files: dict[str, bytes],
        limits: SandboxLimits,
        image: str,
        job_id: str,
    ) -> SandboxOutcome:
        rundir = Path(tempfile.mkdtemp(prefix=f"{job_id}-", dir=self.root))
        for name, data in files.items():
            target = rundir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        # Emulate the container's mount point: the same env strings, with
        # /workspace pointing at this run's directory instead of a tmpfs.
        local_env = {
            key: value.replace(WORKSPACE, str(rundir))
            for key, value in env.items()
        }
        if "PYTHONPATH" in local_env:
            # The container joins with ":"; the host may use ";" (Windows).
            local_env["PYTHONPATH"] = os.pathsep.join(
                local_env["PYTHONPATH"].split(":")
            )
        started = time.monotonic()
        timed_out = False
        exit_code: int | None = None
        stderr = b""
        try:
            completed = subprocess.run(  # noqa: S603 - argv list, no shell
                argv,
                cwd=str(rundir),
                env=local_env,
                capture_output=True,
                timeout=max(1.0, limits.wall_seconds),
            )
            exit_code = completed.returncode
            stderr = completed.stderr
        except subprocess.TimeoutExpired:
            timed_out = True
        out: dict[str, bytes] = {}
        for path in sorted(rundir.rglob("*")):
            if path.is_file():
                out[path.relative_to(rundir).as_posix()] = path.read_bytes()
        shutil.rmtree(rundir, ignore_errors=True)
        return SandboxOutcome(
            exit_code=exit_code,
            timed_out=timed_out,
            stderr=stderr,
            files=out,
            container_id=None,
            duration=round(time.monotonic() - started, 3),
        )
