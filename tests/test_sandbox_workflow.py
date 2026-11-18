"""The sandbox-test-run workflow (checkpoint 9, task 2), against stubs and
the REAL database: the sandbox_runs UNIQUE constraint and its upsert SQL,
the full four-step run through a real worker, input validation, the
per-worker slot budget, and the earlier-attempt container sweep."""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import replace
from typing import Any

import psycopg
import pytest

from engine import db, install, jobs, patchlog, workflows
from engine.config import Settings
from engine.sandbox import LABEL_PREFIX, RunResult, build_workspace_tar, remove_stale_containers
from engine.sandbox_workflow import (
    SANDBOX_RUN_UPSERT_SQL,
    WORKFLOW_TYPE,
    SandboxSlots,
    SandboxWorkflowError,
    _requirements_from_snapshot,
    make_sandbox_test_run_workflow,
    parse_workflow_input,
    reset_sandbox_slots,
    sandbox_slots,
)
from engine.worker import Worker
from engine.workspace import SNAPSHOT_MAX_BYTES

_REQ_DEFAULT = b"six==1.16.0 --hash=sha256:" + b"1" * 60 + b"\n"
_BASE_DIGEST = "c" * 64
_DEPS_IMAGE = "sha256:" + "d" * 64


# ---------------------------------------------------------------------------
# fixtures and stubs
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(clean_db: Settings) -> Any:
    with db.connect(clean_db, application_name="test-sandbox-workflow") as connection:
        yield connection


def _worker_settings(settings: Settings, *, max_attempts: int = 5) -> Settings:
    return replace(
        settings,
        max_attempts=max_attempts,
        backoff_base_seconds=0.05,
        backoff_cap_seconds=0.5,
        idle_sleep_min_seconds=0.02,
        idle_sleep_max_seconds=0.05,
    )


def _install_result(requirements: bytes) -> install.InstallResult:
    return install.InstallResult(
        install_key=install.install_key(requirements, _BASE_DIGEST),
        image=_DEPS_IMAGE,
        base_ref="python@sha256:" + _BASE_DIGEST,
        base_digest=_BASE_DIGEST,
        cached=False,
        duration_seconds=0.2,
    )


class _ListClient:
    """Stub for remove_stale_containers: labeled containers, recorded removes."""

    def __init__(self, containers: list[Any] | None = None) -> None:
        self.containers = list(containers or [])
        self.filters_seen: list[dict[str, Any]] = []

    def list_containers(self, filters: dict[str, Any]) -> list[Any]:
        self.filters_seen.append(filters)
        return list(self.containers)


class _StubContainer:
    def __init__(self, labels: dict[str, str], *, explode: bool = False) -> None:
        self.labels = labels
        self.explode = explode
        self.removed = 0

    def remove(self, *, force: bool = False) -> None:
        self.removed += 1
        if self.explode:
            raise OSError("device or resource busy")


def _wait_until(predicate: Any, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition not reached in time")


def _count(conn: Any, table: str) -> int:
    return int(conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"])


def _row(conn: Any, sql: str, params: Any = None) -> Any:
    return conn.execute(sql, params or {}).fetchone()


def _workflow_state(conn: Any, workflow_id: Any) -> Any:
    return _row(conn, "SELECT state FROM workflows WHERE id = %(id)s", {"id": workflow_id})[
        "state"
    ]


def _step_output(conn: Any, workflow_id: Any, name: str) -> Any:
    return _row(
        conn,
        "SELECT output FROM workflow_steps WHERE workflow_id = %(id)s AND name = %(name)s",
        {"id": workflow_id, "name": name},
    )["output"]


def _current_job(conn: Any, workflow_id: Any, position: int) -> Any:
    return _row(
        conn,
        "SELECT state, attempts, last_error FROM jobs WHERE dedupe_key = %(k)s",
        {"k": workflows.step_job_dedupe_key(workflow_id, position)},
    )


def _run_workflow(
    conn: Any,
    settings: Settings,
    definition: Any,
    wf: Any,
) -> tuple[Worker, threading.Thread]:
    worker = Worker(
        settings,
        workflows.make_workflow_handlers({WORKFLOW_TYPE: definition}, settings=settings),
        application_name="engine-worker-test",
    )
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    return worker, thread


def _stop(worker: Worker, thread: threading.Thread) -> None:
    worker.request_stop()
    thread.join(timeout=10)
    assert not thread.is_alive()


# ---------------------------------------------------------------------------
# the sandbox_runs UNIQUE constraint (the real database constraint)
# ---------------------------------------------------------------------------


def test_sandbox_runs_unique_is_a_real_constraint(conn: Any) -> None:
    """The (job_id, attempt) UNIQUE is a TABLE CONSTRAINT, enforced by the
    database even for a raw INSERT that bypasses the upsert SQL: the
    idempotency of a redelivered report step rests on the constraint, not
    on application code."""
    job = jobs.enqueue(conn, job_type="workflow_step", payload={})

    def insert_run(attempt: int) -> None:
        conn.execute(
            "INSERT INTO sandbox_runs (job_id, attempt, image, exit_code)"
            " VALUES (%(job_id)s, %(attempt)s, %(image)s, %(exit_code)s)",
            {"job_id": job.id, "attempt": attempt, "image": "sha256:x", "exit_code": 0},
        )

    insert_run(1)
    with pytest.raises(psycopg.errors.UniqueViolation) as excinfo:
        with conn.transaction():
            insert_run(1)  # the same execution recorded twice: the DB refuses
    assert excinfo.value.diag.constraint_name == "sandbox_runs_job_id_attempt_key"
    insert_run(2)  # a new attempt is a NEW execution: its own row
    assert _count(conn, "sandbox_runs") == 2


def test_sandbox_run_upsert_is_idempotent(conn: Any) -> None:
    """A retried/redelivered report step rewrites its own row (same id);
    a new attempt adds a second row."""
    job = jobs.enqueue(conn, job_type="workflow_step", payload={})

    def record(attempt: int) -> Any:
        return conn.execute(
            SANDBOX_RUN_UPSERT_SQL,
            {
                "job_id": str(job.id),
                "attempt": attempt,
                "workflow_id": None,
                "workspace_id": None,
                "install_key": install.install_key(_REQ_DEFAULT, _BASE_DIGEST),
                "install_image": _DEPS_IMAGE,
                "install_cached": False,
                "install_duration_ms": 200,
                "image": _DEPS_IMAGE,
                "exit_code": 0,
                "timed_out": False,
                "oom_killed": False,
                "kill_failed": False,
                "stdout_sha256": hashlib.sha256(b"out").hexdigest(),
                "stderr_sha256": None,
                "output_truncated": False,
                "output_summary": "out",
                "duration_ms": 50,
            },
        ).fetchone()

    first = record(1)
    second = record(1)  # the SAME execution, redelivered report step
    assert first["id"] == second["id"]
    assert _count(conn, "sandbox_runs") == 1
    third = record(2)  # a NEW attempt: new row, new id
    assert third["id"] != first["id"]
    assert _count(conn, "sandbox_runs") == 2


# ---------------------------------------------------------------------------
# input validation
# ---------------------------------------------------------------------------


def test_parse_input_rejects_structurally_bad_input() -> None:
    good = {"snapshot_hash": "a" * 64, "command": ["python", "-m", "pytest"]}
    parsed = parse_workflow_input(good, allowed_commands=None)
    assert parsed.command == ("python", "-m", "pytest")
    assert parsed.requirements_path == "requirements.txt"

    with pytest.raises(SandboxWorkflowError, match="sha256"):
        parse_workflow_input({**good, "snapshot_hash": "nothex"}, allowed_commands=None)
    with pytest.raises(SandboxWorkflowError, match="argv list"):
        parse_workflow_input({**good, "command": "python -m pytest"}, allowed_commands=None)
    with pytest.raises(SandboxWorkflowError, match="argv list"):
        parse_workflow_input({**good, "command": []}, allowed_commands=None)
    with pytest.raises(SandboxWorkflowError, match="token"):
        parse_workflow_input({**good, "command": ["python", ""]}, allowed_commands=None)
    with pytest.raises(SandboxWorkflowError, match="allowlist"):
        parse_workflow_input(
            {**good, "command": ["bash", "-c", "x"]},
            allowed_commands=(("python", "-m", "pytest"),),
        )
    with pytest.raises(SandboxWorkflowError, match="requirements_path"):
        parse_workflow_input(
            {**good, "requirements_path": "../escape.txt"}, allowed_commands=None
        )


def test_requirements_digest_is_reverified_from_the_blob(conn: Any) -> None:
    """Install re-reads the pins from the immutable snapshot and verifies
    prepare's digest — the content never rides in a step output."""
    requirements = _REQ_DEFAULT
    files = {"requirements.txt": requirements, "src/main.py": b"x\n"}
    snapshot_hash = patchlog.store_blob(
        conn, build_workspace_tar(files), cap_bytes=SNAPSHOT_MAX_BYTES
    )
    digest = hashlib.sha256(requirements).hexdigest()
    content = _requirements_from_snapshot(conn, snapshot_hash, "requirements.txt", digest)
    assert content == requirements
    with pytest.raises(SandboxWorkflowError, match="digest changed"):
        _requirements_from_snapshot(conn, snapshot_hash, "requirements.txt", "f" * 64)
    with pytest.raises(SandboxWorkflowError, match="no file"):
        _requirements_from_snapshot(conn, snapshot_hash, "absent.txt", digest)


# ---------------------------------------------------------------------------
# the full four-step workflow through a real worker
# ---------------------------------------------------------------------------


def test_full_workflow_records_a_sandbox_run(
    conn: Any, clean_db: Settings, monkeypatch: Any
) -> None:
    """prepare -> install -> run -> report: outputs chain, the record is
    keyed by the RUN step's (job_id, attempt), and no patch rows exist —
    a test run is a measurement, not a workspace effect."""
    reset_sandbox_slots()
    settings = _worker_settings(clean_db)
    requirements = _REQ_DEFAULT
    files = {"requirements.txt": requirements, "src/main.py": b"print('main')\n"}
    snapshot_hash = patchlog.store_blob(
        conn, build_workspace_tar(files), cap_bytes=SNAPSHOT_MAX_BYTES
    )

    client = _ListClient()
    install_calls: dict[str, Any] = {}

    def recording_ensure(client_: Any, **kwargs: Any) -> install.InstallResult:
        install_calls.update(kwargs)
        return _install_result(kwargs["requirements"])

    run_calls: dict[str, Any] = {}

    def fake_run(spec: Any, client_: Any, limits: Any = None) -> RunResult:
        run_calls["spec"] = spec
        return RunResult(
            job_id=spec.job_id,
            attempt=spec.attempt,
            container_id="c0ffee",
            exit_code=3,  # a failing test suite is an OUTCOME, not a failure
            timed_out=False,
            oom_killed=False,
            kill_failed=False,
            stdout=b"3 failed, 12 passed\n",
            stderr=b"",
            stdout_truncated=False,
            stderr_truncated=False,
            files={},
            symlinks_skipped=0,
            duration_seconds=0.123,
        )

    monkeypatch.setattr("engine.sandbox_workflow.ensure_dependency_image", recording_ensure)
    monkeypatch.setattr("engine.sandbox_workflow.run_sandbox", fake_run)

    definition = make_sandbox_test_run_workflow(
        client,
        settings=settings,
        allowed_commands=(("pytest", "-q"),),
    )
    wf = workflows.start_workflow(
        conn,
        wf_type=WORKFLOW_TYPE,
        definition=definition,
        input={"snapshot_hash": snapshot_hash, "command": ["pytest", "-q"]},
    )
    worker, thread = _run_workflow(conn, settings, definition, wf)
    try:
        _wait_until(lambda: _workflow_state(conn, wf.id) == "succeeded")
        slots = sandbox_slots(settings)

        # prepare: workspace identity + the pins' digest (content stays out)
        prepare = _step_output(conn, wf.id, "prepare")
        assert prepare["snapshot_hash"] == snapshot_hash
        assert prepare["requirements_sha256"] == hashlib.sha256(requirements).hexdigest()
        assert prepare["file_count"] == 2
        workspace_id = prepare["workspace_id"]

        # install: the stub saw the requirements RE-READ from the snapshot
        # (not passed through step outputs) and the settings' base image
        assert install_calls["requirements"] == requirements
        assert install_calls["base_image"] == settings.install_base_image
        install_out = _step_output(conn, wf.id, "install")
        expected_key = install.install_key(requirements, _BASE_DIGEST)
        assert install_out["install_key"] == expected_key
        assert install_out["image"] == _DEPS_IMAGE
        assert install_out["cached"] is False

        # run: the spec carried the DEPENDENCY image, the argv, and the env
        spec = run_calls["spec"]
        assert spec.image == _DEPS_IMAGE
        assert spec.argv == ("pytest", "-q")
        assert spec.env == {"PYTHONPATH": "/var/tmp/deps"}
        assert spec.workspace_files == files
        run_out = _step_output(conn, wf.id, "run")
        assert run_out["exit_code"] == 3
        assert run_out["stdout_sha256"] == hashlib.sha256(b"3 failed, 12 passed\n").hexdigest()
        assert run_out["files_discarded"] is True
        run_job_id = run_out["run_job_id"]

        # report: the record keyed by the RUN step's execution identity
        record = _row(
            conn,
            "SELECT * FROM sandbox_runs WHERE job_id = %(j)s AND attempt = 1",
            {"j": run_job_id},
        )
        assert record is not None
        assert str(record["job_id"]) == run_job_id
        assert record["attempt"] == 1
        assert record["workflow_id"] == wf.id
        assert record["workspace_id"] == workspace_id
        assert record["install_key"] == expected_key
        assert record["image"] == _DEPS_IMAGE
        assert record["exit_code"] == 3
        assert record["stdout_sha256"] == run_out["stdout_sha256"]
        assert record["output_summary"] == "3 failed, 12 passed"
        assert record["duration_ms"] == 123

        # a measurement, not an effect: the patch log is untouched
        assert _count(conn, "workspace_patches") == 0
        assert slots.active == 0  # the slot was released
    finally:
        _stop(worker, thread)


def test_workflow_job_fanout_is_one_job_per_step(
    conn: Any, clean_db: Settings, monkeypatch: Any
) -> None:
    """Four steps, four jobs, one run record; the step names in order."""
    settings = _worker_settings(clean_db)
    files = {"requirements.txt": _REQ_DEFAULT, "src/main.py": b"x\n"}
    snapshot_hash = patchlog.store_blob(
        conn, build_workspace_tar(files), cap_bytes=SNAPSHOT_MAX_BYTES
    )
    client = _ListClient()
    monkeypatch.setattr(
        "engine.sandbox_workflow.ensure_dependency_image",
        lambda client_, **kwargs: _install_result(kwargs["requirements"]),
    )
    monkeypatch.setattr(
        "engine.sandbox_workflow.run_sandbox",
        lambda spec, client_, limits=None: RunResult(
            job_id=spec.job_id,
            attempt=spec.attempt,
            container_id=None,
            exit_code=0,
            timed_out=False,
            oom_killed=False,
            kill_failed=False,
            stdout=b"ok\n",
            stderr=b"",
            stdout_truncated=False,
            stderr_truncated=False,
            files={},
            symlinks_skipped=0,
            duration_seconds=0.01,
        ),
    )
    definition = make_sandbox_test_run_workflow(client, settings=settings)
    wf = workflows.start_workflow(
        conn,
        wf_type=WORKFLOW_TYPE,
        definition=definition,
        input={"snapshot_hash": snapshot_hash, "command": ["python", "src/main.py"]},
    )
    worker, thread = _run_workflow(conn, settings, definition, wf)
    try:
        _wait_until(lambda: _workflow_state(conn, wf.id) == "succeeded")
        names = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM workflow_steps WHERE workflow_id = %(id)s ORDER BY position",
                {"id": wf.id},
            ).fetchall()
        ]
        assert names == ["prepare", "install", "run", "report"]
        assert _count(conn, "workflow_steps") == 4
        assert _count(conn, "jobs") == 4  # one job per step, nothing leaked
        assert _count(conn, "sandbox_runs") == 1
    finally:
        _stop(worker, thread)


def test_unknown_command_dead_letters_without_burning_retries(
    conn: Any, clean_db: Settings
) -> None:
    """A command outside the exact allowlist is permanent: NonRetryable on
    the FIRST attempt (the dead-letter path, not a retry loop)."""
    settings = _worker_settings(clean_db, max_attempts=5)
    snapshot_hash = patchlog.store_blob(
        conn,
        build_workspace_tar({"requirements.txt": _REQ_DEFAULT}),
        cap_bytes=SNAPSHOT_MAX_BYTES,
    )
    definition = make_sandbox_test_run_workflow(
        _ListClient(), settings=settings, allowed_commands=(("pytest", "-q"),)
    )
    wf = workflows.start_workflow(
        conn,
        wf_type=WORKFLOW_TYPE,
        definition=definition,
        input={"snapshot_hash": snapshot_hash, "command": ["bash", "-c", "rm -rf /"]},
    )
    worker, thread = _run_workflow(conn, settings, definition, wf)
    try:
        _wait_until(lambda: (_current_job(conn, wf.id, 0) or {"state": ""})["state"] == "dead")
        job = _current_job(conn, wf.id, 0)
        assert job["attempts"] == 1, "permanent refusal does not retry"
        assert "non-retryable" in (job["last_error"] or "")
        assert "allowlist" in (job["last_error"] or "")
        assert _step_output(conn, wf.id, "prepare") is None
    finally:
        _stop(worker, thread)


def test_install_permanent_vs_transient_failures(
    conn: Any, clean_db: Settings, monkeypatch: Any
) -> None:
    """pip_failed/too_large/base_image_missing dead-letter NOW; timeout/oom/
    daemon/lock_timeout retry with backoff (attempts burn, then dead)."""
    settings = _worker_settings(clean_db, max_attempts=2)
    snapshot_hash = patchlog.store_blob(
        conn,
        build_workspace_tar({"requirements.txt": _REQ_DEFAULT}),
        cap_bytes=SNAPSHOT_MAX_BYTES,
    )

    def run_with(code: str) -> tuple[Any, int]:
        definition = make_sandbox_test_run_workflow(_ListClient(), settings=settings)
        wf = workflows.start_workflow(
            conn,
            wf_type=WORKFLOW_TYPE,
            definition=definition,
            input={"snapshot_hash": snapshot_hash, "command": ["pytest", "-q"]},
        )
        calls: dict[str, int] = {}

        def flaky_ensure(client_: Any, **kwargs: Any) -> install.InstallResult:
            calls["n"] = calls.get("n", 0) + 1
            raise install.InstallFailed(code, f"stub {code} failure")

        monkeypatch.setattr("engine.sandbox_workflow.ensure_dependency_image", flaky_ensure)
        worker, thread = _run_workflow(conn, settings, definition, wf)
        try:
            _wait_until(
                lambda: (_current_job(conn, wf.id, 1) or {"state": ""})["state"] == "dead",
                timeout=30.0,
            )
            return _current_job(conn, wf.id, 1), calls["n"]
        finally:
            _stop(worker, thread)

    job, calls = run_with("pip_failed")
    assert job["attempts"] == 1, "permanent install failure dead-letters immediately"
    assert calls == 1
    assert "install failed permanently" in (job["last_error"] or "")

    job, calls = run_with("timeout")
    # the retry budget is the STEP's own snapshot (StepDef default 5); what
    # the distinction buys is: transient RETRIES (attempts grew past 1)
    # instead of dead-lettering on the first attempt
    assert job["attempts"] >= 2
    assert calls == job["attempts"]


# ---------------------------------------------------------------------------
# the slot budget (task 4) and the stale-container sweep
# ---------------------------------------------------------------------------


def test_sandbox_slots_bound_concurrency() -> None:
    slots = SandboxSlots(2)
    errors: list[BaseException] = []
    observed: list[int] = []
    gate = threading.Event()

    def work() -> None:
        try:
            with slots.slot():
                observed.append(slots.active)
                gate.wait(timeout=5)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(6)]
    for thread in threads:
        thread.start()
    time.sleep(0.1)
    gate.set()
    for thread in threads:
        thread.join(timeout=10)
    assert not errors
    assert max(observed) <= 2, "the semaphore bounds concurrent sandboxes"
    assert slots.active == 0
    assert slots.max_active == 2  # exactly the budget, never above
    with pytest.raises(ValueError, match=">= 1"):
        SandboxSlots(0)


def test_sandbox_slots_are_sized_from_settings(clean_db: Settings) -> None:
    reset_sandbox_slots()
    slots = sandbox_slots(replace(clean_db, max_concurrent_sandboxes=3))
    assert slots.size == 3
    assert sandbox_slots(replace(clean_db, max_concurrent_sandboxes=3)) is slots
    assert sandbox_slots(replace(clean_db, max_concurrent_sandboxes=7)).size == 7
    reset_sandbox_slots()


def test_remove_stale_containers_sweeps_earlier_attempts() -> None:
    stale = _StubContainer({LABEL_PREFIX: "1", f"{LABEL_PREFIX}.attempt": "0"})
    current = _StubContainer({LABEL_PREFIX: "1", f"{LABEL_PREFIX}.attempt": "1"})
    garbled = _StubContainer({LABEL_PREFIX: "1", f"{LABEL_PREFIX}.attempt": "x"})
    unlabeled = _StubContainer({LABEL_PREFIX: "1"})
    wedged = _StubContainer(
        {LABEL_PREFIX: "1", f"{LABEL_PREFIX}.attempt": "0"}, explode=True
    )
    client = _ListClient([stale, current, garbled, unlabeled, wedged])
    removed = remove_stale_containers(client, job_id="job-9", keep_attempt=1)
    # exactly one list call, filtered to THIS job's containers
    assert client.filters_seen == [{"label": f"{LABEL_PREFIX}.job=job-9"}]
    assert stale.removed == 1
    assert wedged.removed == 1  # removal attempted; the OSError was tolerated
    assert removed == 1  # only SUCCESSFUL removals count; the janitor retries
    assert current.removed == 0  # keep_attempt keeps this attempt's own
    assert garbled.removed == 0  # garbled labels: the janitor's rules, not ours
    assert unlabeled.removed == 0
