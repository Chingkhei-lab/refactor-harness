"""Worker loop tests: retries, unknown types, lease loss, connection kills."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from engine import db, jobs
from engine.config import Settings
from engine.worker import HandlerContext, Worker

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _lease_settings(settings: Settings, lease: float) -> Settings:
    return replace(
        settings, lease_seconds=lease, heartbeat_interval_seconds=lease / 3
    )


def _run_worker(
    settings: Settings, handlers: dict[str, Any], application_name: str
) -> tuple[Worker, threading.Thread]:
    worker = Worker(settings, handlers, application_name=application_name)
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    return worker, thread


def _stop(worker: Worker, thread: threading.Thread) -> None:
    worker.request_stop()
    thread.join(timeout=10)
    assert not thread.is_alive()


def _wait_until(predicate: Any, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition not reached in time")


def _row(conn: Any, sql: str) -> Any:
    row = conn.execute(sql).fetchone()
    assert row is not None
    return row


def test_worker_retries_after_a_handler_exception(clean_db: Settings) -> None:
    settings = replace(
        clean_db,
        backoff_base_seconds=0.001,
        idle_sleep_min_seconds=0.02,
        idle_sleep_max_seconds=0.05,
    )
    conn = db.connect(settings)

    def boom(payload: dict[str, Any], ctx: HandlerContext) -> dict[str, Any] | None:
        raise RuntimeError("kaboom")

    jobs.enqueue(conn, job_type="boom", payload={}, max_attempts=2)
    worker, thread = _run_worker(settings, {"boom": boom}, "engine-worker-test")
    try:
        _wait_until(
            lambda: _row(conn, "SELECT state FROM jobs LIMIT 1")["state"] == "dead"
        )
        row = _row(conn, "SELECT state, attempts, last_error FROM jobs LIMIT 1")
        assert row["state"] == "dead"
        assert row["attempts"] == 2
        assert "kaboom" in str(row["last_error"])
    finally:
        _stop(worker, thread)


def test_worker_never_claims_an_unknown_type(clean_db: Settings) -> None:
    settings = replace(
        clean_db, idle_sleep_min_seconds=0.02, idle_sleep_max_seconds=0.05
    )
    conn = db.connect(settings)
    jobs.enqueue(conn, job_type="unknown", payload={})
    jobs.enqueue(conn, job_type="known", payload={})

    worker, thread = _run_worker(
        settings, {"known": lambda payload, ctx: {"ok": True}}, "engine-worker-test"
    )
    try:
        _wait_until(
            lambda: _row(
                conn,
                "SELECT count(*) AS n FROM jobs WHERE state = 'succeeded'",
            )["n"]
            == 1
        )
        row = _row(
            conn,
            "SELECT state, attempts FROM jobs WHERE type = 'unknown'",
        )
        assert row["state"] == "queued", "unknown types must never be claimed"
        assert row["attempts"] == 0, "unknown types must never burn an attempt"
    finally:
        _stop(worker, thread)


def test_lease_lost_mid_handler_writes_no_result(clean_db: Settings) -> None:
    settings = _lease_settings(clean_db, 1.2)  # interval = 0.4s
    conn = db.connect(settings)
    started = threading.Event()
    release = threading.Event()

    def hang(payload: dict[str, Any], ctx: HandlerContext) -> dict[str, Any] | None:
        started.set()
        assert release.wait(10)
        deadline = time.monotonic() + 10
        while not ctx.lease_lost.is_set() and time.monotonic() < deadline:
            time.sleep(0.02)
        return {"ran": True}

    jobs.enqueue(conn, job_type="hang", payload={})
    worker, thread = _run_worker(settings, {"hang": hang}, "engine-worker-test")
    try:
        _wait_until(lambda: started.is_set())
        _wait_until(
            lambda: _row(conn, "SELECT state FROM jobs LIMIT 1")["state"] == "running"
        )
        # mimic the reaper; push run_at far out so this worker cannot re-claim
        conn.execute(
            "UPDATE jobs SET state = 'queued', run_at = now() + interval '1 hour',"
            " locked_by = NULL, lease_expires_at = NULL, last_heartbeat_at = NULL"
            " WHERE state = 'running'"
        )
        release.set()
        _wait_until(
            lambda: _row(conn, "SELECT result, state FROM jobs LIMIT 1")["state"]
            == "queued"
        )
        row = _row(conn, "SELECT state, result, attempts FROM jobs LIMIT 1")
        assert row["result"] is None, "a lease-lost attempt must not write a result"
        assert row["attempts"] == 1
    finally:
        _stop(worker, thread)


def test_worker_survives_a_terminated_backend(clean_db: Settings) -> None:
    settings = replace(
        clean_db, idle_sleep_min_seconds=0.02, idle_sleep_max_seconds=0.1
    )
    conn = db.connect(settings)
    jobs.enqueue(conn, job_type="ok", payload={})
    worker, thread = _run_worker(
        settings, {"ok": lambda payload, ctx: {"ok": True}}, "engine-worker-test"
    )
    try:
        _wait_until(
            lambda: _row(
                conn,
                "SELECT count(*) AS n FROM jobs WHERE state = 'succeeded'",
            )["n"]
            == 1
        )
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
            " WHERE application_name = 'engine-worker-test'"
            " AND pid <> pg_backend_pid()"
        )
        jobs.enqueue(conn, job_type="ok", payload={})
        _wait_until(
            lambda: _row(
                conn,
                "SELECT count(*) AS n FROM jobs WHERE state = 'succeeded'",
            )["n"]
            == 2
        )
        assert thread.is_alive(), "the worker must reconnect, not crash"
    finally:
        _stop(worker, thread)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows has no reliable SIGTERM; this test runs on Linux (CI/WSL/Docker)",
)
def test_sigterm_drains_the_current_job_and_exits_0(clean_db: Settings) -> None:
    conn = db.connect(clean_db)
    jobs.enqueue(conn, job_type="slow", payload={})

    env = os.environ.copy()
    env["DATABASE_URL"] = clean_db.database_url
    env["ENGINE_HANDLERS_MODULE"] = str(_REPO_ROOT / "tests" / "slow_handlers.py")
    env["ENGINE_IDLE_SLEEP_MIN_SECONDS"] = "0.05"
    env["ENGINE_IDLE_SLEEP_MAX_SECONDS"] = "0.2"
    env["PYTHONPATH"] = (
        str(_REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "engine.worker"], cwd=_REPO_ROOT, env=env
    )
    try:
        _wait_until(
            lambda: _row(conn, "SELECT state FROM jobs LIMIT 1")["state"] == "running"
        )
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=30) == 0, "graceful drain must exit 0"
        row = _row(conn, "SELECT state FROM jobs LIMIT 1")
        assert row["state"] == "succeeded", "the current job must finish"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
