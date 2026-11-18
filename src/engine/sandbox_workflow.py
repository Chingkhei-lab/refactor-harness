"""The sandbox-test-run workflow (checkpoint 9, task 2): four durable steps.

prepare -> install -> run -> report, one JOB per step (Phase 2 machinery).
The invariant is the advance transaction: each step's output and its
carrying job's completion commit together, so a killed worker loses only
the in-flight attempt.

- prepare: the repository snapshot (a blob, e.g. `git archive` piped to the
  caller) is parsed with the safe tar extractor and becomes a workspace.
  Its output carries the workspace identity and the requirements file's
  digest — the file's CONTENT stays in the blob store, never in a JSONB
  output (the tool_calls lesson: repo content does not go in rows).
- install: ensures the dependency image exists for
  sha256(requirements + base digest) and outputs its actual image ID.
- run: removes earlier attempts' containers, copies the workspace into a
  sandbox FROM that image (PYTHONPATH=/var/tmp/deps), and runs the
  allowlisted command under the runner's limits. Its output is the
  MEASUREMENT: exit code, flags, digests of the truncated streams, a short
  summary. A failing test suite is a recorded outcome, not a workflow
  failure — the run record stores what happened.
- report: upserts the sandbox_runs row (keyed by the RUN step's
  job_id + attempt) inside the advance transaction, so the record commits
  atomically with the workflow's progress.

Phase-2 difference, stated: sandbox results are MEASUREMENTS, not effects.
A worker killed mid-run leaves no run-record row for that attempt (the
output was never committed); the retry produces a NEW execution and its
own row. Outputs are not promised to be identical across attempts — the
workflow's recorded result is the committing attempt's.
"""

from __future__ import annotations

import contextlib
import hashlib
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from . import jobs, patchlog
from .config import Settings
from .install import (
    InstallFailed,
    InstallLimits,
    InstallResult,
    ensure_dependency_image,
)
from .paths import PathDenied, is_sha256_hex, validate_path
from .sandbox import (
    SandboxClient,
    SandboxLimits,
    SandboxSpec,
    parse_workspace_tar,
    remove_stale_containers,
)
from .sandbox import (
    run as run_sandbox,
)
from .workflows import StepContext, StepDef, WorkflowDef
from .workspace import SNAPSHOT_MAX_BYTES

WORKFLOW_TYPE = "sandbox-test-run"
_HERE = Path(__file__).parent
_SQL_DIR = _HERE / "sql"
# The sandbox_runs upsert: the record contract (keyed by the RUN step's
# (job_id, attempt), idempotent via ON CONFLICT). Public: tests pin it.
SANDBOX_RUN_UPSERT_SQL = (_SQL_DIR / "sandbox_run_upsert.sql").read_text(encoding="utf-8")
_DEPS_DIR = "/var/tmp/deps"
_OUTPUT_SUMMARY_CHARS = 200
_MAX_COMMAND_TOKENS = 64
_MAX_TOKEN_CHARS = 200

# Permanent install failures: bad pins / oversized pins / missing base
# image. Everything else (timeout, oom, daemon, lock_timeout) retries.
_PERMANENT_INSTALL_CODES = frozenset({"pip_failed", "too_large", "base_image_missing"})


class SandboxWorkflowError(RuntimeError):
    """A permanent workflow-input problem (bad snapshot, unknown command)."""


# ---------------------------------------------------------------------------
# per-worker sandbox slots (task 4)
# ---------------------------------------------------------------------------


class SandboxSlots:
    """The per-worker sandbox budget: a bounded semaphore + observability.

    Today's worker runs one job at a time, so one process holds at most one
    sandbox; the semaphore is the enforced budget for a host that runs
    several worker processes and for a future threaded worker. The default
    comes from the measured host maximum (task 4). `max_active` records the
    high-water mark for the measurement report.
    """

    def __init__(self, size: int) -> None:
        if size < 1:
            raise ValueError(f"sandbox slot count must be >= 1, got {size}")
        self._sem = threading.BoundedSemaphore(size)
        self.size = size
        self.active = 0
        self.max_active = 0

    @contextlib.contextmanager
    def slot(self) -> Iterator[None]:
        self._sem.acquire()
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            yield
        finally:
            self.active -= 1
            self._sem.release()


_SLOTS: SandboxSlots | None = None
_SLOTS_LOCK = threading.Lock()


def sandbox_slots(settings: Settings) -> SandboxSlots:
    """The process-wide slot budget, lazily sized from settings."""
    global _SLOTS
    with _SLOTS_LOCK:
        if _SLOTS is None or _SLOTS.size != settings.max_concurrent_sandboxes:
            _SLOTS = SandboxSlots(settings.max_concurrent_sandboxes)
        return _SLOTS


def reset_sandbox_slots() -> None:
    """Test hook: drop the cached budget so a new size takes effect."""
    global _SLOTS
    with _SLOTS_LOCK:
        _SLOTS = None


# ---------------------------------------------------------------------------
# input parsing + validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SandboxTestRunInput:
    snapshot_hash: str
    command: tuple[str, ...]
    requirements_path: str = "requirements.txt"


def _non_retryable(message: str) -> jobs.NonRetryableError:
    return jobs.NonRetryableError(message)


class SandboxRunnerClient(SandboxClient, Protocol):
    """What this workflow's client must provide: the sandbox duck-type plus
    the install phase's image inspect — the Docker adapter carries both; the
    test stubs carry both."""

    def image_attrs(self, ref: str) -> dict[str, Any] | None: ...


def parse_workflow_input(
    raw: dict[str, Any], *, allowed_commands: tuple[tuple[str, ...], ...] | None
) -> SandboxTestRunInput:
    """Structural validation of the operator's workflow input.

    The command is an argv list, never a shell string; when an exact
    allowlist is configured the command must match one entry verbatim.
    """
    snapshot_hash = raw.get("snapshot_hash")
    if not isinstance(snapshot_hash, str) or not is_sha256_hex(snapshot_hash):
        raise SandboxWorkflowError("input.snapshot_hash must be a sha256 hex digest")
    command_raw = raw.get("command")
    if (
        not isinstance(command_raw, list)
        or not command_raw
        or len(command_raw) > _MAX_COMMAND_TOKENS
        or not all(isinstance(token, str) for token in command_raw)
    ):
        raise SandboxWorkflowError(
            f"input.command must be a non-empty argv list of at most {_MAX_COMMAND_TOKENS} strings"
        )
    for token in command_raw:
        if not token or len(token) > _MAX_TOKEN_CHARS or "\x00" in token:
            raise SandboxWorkflowError(
                f"input.command token over {_MAX_TOKEN_CHARS} chars, empty, or NUL"
            )
    command = tuple(command_raw)
    if allowed_commands is not None and command not in allowed_commands:
        raise SandboxWorkflowError(
            f"command {command!r} is not in the workflow's allowlist "
            f"({len(allowed_commands)} entries)"
        )
    requirements_path = raw.get("requirements_path", "requirements.txt")
    if not isinstance(requirements_path, str):
        raise SandboxWorkflowError("input.requirements_path must be a string")
    try:
        requirements_path = validate_path(requirements_path)
    except PathDenied as exc:
        raise SandboxWorkflowError(f"input.requirements_path: {exc}") from exc
    return SandboxTestRunInput(
        snapshot_hash=snapshot_hash, command=command, requirements_path=requirements_path
    )


def _workflow_input(
    ctx: StepContext, allowed_commands: tuple[tuple[str, ...], ...] | None
) -> SandboxTestRunInput:
    if ctx.conn is None:
        raise RuntimeError(f"step {ctx.step!r} has no connection")
    row = ctx.conn.execute(
        "SELECT input FROM workflows WHERE id = %s", (ctx.workflow_id,)
    ).fetchone()
    if row is None:
        raise RuntimeError(f"workflow {ctx.workflow_id} vanished")
    try:
        return parse_workflow_input(dict(row["input"] or {}), allowed_commands=allowed_commands)
    except SandboxWorkflowError as exc:
        # a malformed input is permanent: dead-letter now, don't burn retries
        raise _non_retryable(str(exc)) from exc


def _requirements_from_snapshot(
    conn: Any, snapshot_hash: str, path: str, expected_sha256: str
) -> bytes:
    """Re-read the pins from the snapshot blob, verifying prepare's digest."""
    data = patchlog.load_blob(conn, snapshot_hash)
    files, _skipped = parse_workspace_tar(data, max_bytes=SNAPSHOT_MAX_BYTES, max_entries=20_000)
    content = files.get(path)
    if content is None:
        raise SandboxWorkflowError(f"snapshot has no file {path!r}")
    digest = hashlib.sha256(content).hexdigest()
    if expected_sha256 and digest != expected_sha256:
        raise SandboxWorkflowError(
            f"requirements digest changed between prepare and install "
            f"({expected_sha256} -> {digest}): the snapshot blob is immutable, "
            "so this is a bug, not a race"
        )
    return content


# ---------------------------------------------------------------------------
# the four steps
# ---------------------------------------------------------------------------


def make_sandbox_test_run_workflow(
    client: SandboxRunnerClient,
    *,
    settings: Settings,
    allowed_commands: tuple[tuple[str, ...], ...] | None = None,
    sandbox_limits: SandboxLimits | None = None,
    install_limits: InstallLimits | None = None,
) -> WorkflowDef:
    """The sandbox-test-run WorkflowDef, closed over the docker client.

    `client` serves both phases: the sandbox duck-type (create/list/exec)
    and the install duck-type (create/image_attrs) — the Docker adapter
    provides both; tests provide stubs.
    """

    def prepare_step(ctx: StepContext, prior: dict[str, Any]) -> dict[str, Any]:
        del prior  # first step
        payload = _workflow_input(ctx, allowed_commands)
        assert ctx.conn is not None
        try:
            snapshot = patchlog.load_blob(ctx.conn, payload.snapshot_hash)
        except patchlog.BlobMissing as exc:
            raise _non_retryable(f"snapshot blob {payload.snapshot_hash} not found") from exc
        try:
            files, _skipped = parse_workspace_tar(
                snapshot,
                max_bytes=SNAPSHOT_MAX_BYTES,
                max_entries=20_000,
            )
        except Exception as exc:  # hostile or oversized snapshot: permanent
            raise _non_retryable(f"snapshot is not a usable workspace: {exc}") from exc
        requirements = files.get(payload.requirements_path)
        if requirements is None:
            raise _non_retryable(
                f"snapshot has no {payload.requirements_path!r}; the install phase cannot be keyed"
            )
        with ctx.conn.transaction():
            workspace_id = patchlog.create_workspace(ctx.conn, files)
        head = ctx.conn.execute(
            "SELECT head_hash FROM workspaces WHERE id = %s", (workspace_id,)
        ).fetchone()
        return {
            "workspace_id": workspace_id,
            "head_hash": str(head["head_hash"]) if head else "",
            "snapshot_hash": payload.snapshot_hash,
            "requirements_path": payload.requirements_path,
            "requirements_sha256": hashlib.sha256(requirements).hexdigest(),
            "file_count": len(files),
        }

    def install_step(ctx: StepContext, prior: dict[str, Any]) -> dict[str, Any]:
        payload = _workflow_input(ctx, allowed_commands)
        prepare = prior.get("prepare")
        if not isinstance(prepare, dict) or "workspace_id" not in prepare:
            raise RuntimeError("install step ran without a prepare output")
        assert ctx.job_id is not None and ctx.conn is not None
        # a killed worker's install container is this job's orphan; the
        # retry sweeps its own job's earlier attempts before building
        remove_stale_containers(client, job_id=str(ctx.job_id), keep_attempt=ctx.attempt)
        requirements = _requirements_from_snapshot(
            ctx.conn,
            payload.snapshot_hash,
            payload.requirements_path,
            str(prepare.get("requirements_sha256", "")),
        )
        try:
            result: InstallResult = ensure_dependency_image(
                client,
                requirements=requirements,
                base_image=settings.install_base_image,
                job_id=str(ctx.job_id),
                attempt=ctx.attempt,
                limits=install_limits,
                lock_dir=settings.install_lock_dir,
            )
        except InstallFailed as exc:
            if exc.code in _PERMANENT_INSTALL_CODES:
                raise _non_retryable(f"install failed permanently: {exc}") from exc
            raise  # timeout / oom / daemon / lock: retry with backoff
        return {
            "install_key": result.install_key,
            "image": result.image,
            "base_ref": result.base_ref,
            "base_digest": result.base_digest,
            "cached": result.cached,
            "duration_ms": int(result.duration_seconds * 1000),
        }

    def run_step(ctx: StepContext, prior: dict[str, Any]) -> dict[str, Any]:
        payload = _workflow_input(ctx, allowed_commands)
        prepare = prior.get("prepare")
        install = prior.get("install")
        if not isinstance(prepare, dict) or not isinstance(install, dict):
            raise RuntimeError("run step ran without prepare/install outputs")
        image = install.get("image")
        if not isinstance(image, str) or not image:
            raise RuntimeError("install output carries no image id")
        assert ctx.job_id is not None and ctx.conn is not None
        rebuilt = patchlog.rebuild_workspace(ctx.conn, int(prepare["workspace_id"]))
        # the slot: the per-worker budget around the only container fan-out
        slots = sandbox_slots(settings)
        with slots.slot():
            # earlier attempts' containers go BEFORE this attempt starts
            remove_stale_containers(client, job_id=str(ctx.job_id), keep_attempt=ctx.attempt)
            spec = SandboxSpec(
                image=image,
                argv=payload.command,
                env={"PYTHONPATH": _DEPS_DIR},
                workspace_files=dict(rebuilt.files),
                job_id=str(ctx.job_id),
                attempt=ctx.attempt,
            )
            result = run_sandbox(spec, client, limits=sandbox_limits)
        stdout_digest = hashlib.sha256(result.stdout).hexdigest()
        stderr_digest = hashlib.sha256(result.stderr).hexdigest()
        summary = result.stdout.decode("utf-8", errors="replace").strip()
        return {
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "oom_killed": result.oom_killed,
            "kill_failed": result.kill_failed,
            "stdout_sha256": stdout_digest,
            "stderr_sha256": stderr_digest,
            "stdout_truncated": result.stdout_truncated,
            "stderr_truncated": result.stderr_truncated,
            "output_summary": summary[:_OUTPUT_SUMMARY_CHARS],
            "symlinks_skipped": result.symlinks_skipped,
            "duration_ms": int(result.duration_seconds * 1000),
            # the run's execution identity: sandbox_runs keys the record on it
            "run_job_id": str(ctx.job_id),
            "run_attempt": ctx.attempt,
            # copied-out workspace mutations are DISCARDED: a test run is a
            # measurement; the workspace's durable state is the patch log
            "files_discarded": True,
        }

    def report_step(ctx: StepContext, prior: dict[str, Any]) -> dict[str, Any]:
        prepare = prior.get("prepare")
        install = prior.get("install")
        run = prior.get("run")
        if not isinstance(prepare, dict):
            raise RuntimeError("report step ran without a prepare output")
        if not isinstance(install, dict):
            raise RuntimeError("report step ran without an install output")
        if not isinstance(run, dict):
            raise RuntimeError("report step ran without a run output")
        run_job_id = run.get("run_job_id")
        run_attempt = run.get("run_attempt")
        if not isinstance(run_job_id, str) or not isinstance(run_attempt, int):
            raise RuntimeError("run output carries no execution identity")
        record: dict[str, Any] = {
            "job_id": run_job_id,
            "attempt": run_attempt,
            "workflow_id": ctx.workflow_id,
            "workspace_id": prepare.get("workspace_id"),
            "install_key": install.get("install_key", ""),
            "install_image": install.get("image", ""),
            "install_cached": bool(install.get("cached", False)),
            "install_duration_ms": install.get("duration_ms"),
            "image": install.get("image", ""),
            "exit_code": run.get("exit_code"),
            "timed_out": bool(run.get("timed_out", False)),
            "oom_killed": bool(run.get("oom_killed", False)),
            "kill_failed": bool(run.get("kill_failed", False)),
            "stdout_sha256": run.get("stdout_sha256"),
            "stderr_sha256": run.get("stderr_sha256"),
            "output_truncated": bool(run.get("stdout_truncated", False))
            or bool(run.get("stderr_truncated", False)),
            "output_summary": run.get("output_summary"),
            "duration_ms": run.get("duration_ms"),
        }

        def _write_record(conn: Any) -> None:
            conn.execute(SANDBOX_RUN_UPSERT_SQL, record)

        ctx.after_complete(_write_record)
        return {"recorded": {"job_id": run_job_id, "attempt": run_attempt}}

    return WorkflowDef(
        type=WORKFLOW_TYPE,
        steps=(
            StepDef(name="prepare", fn=prepare_step),
            StepDef(name="install", fn=install_step),
            StepDef(name="run", fn=run_step),
            StepDef(name="report", fn=report_step),
        ),
    )
