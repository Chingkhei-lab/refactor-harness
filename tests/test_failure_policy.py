"""Checkpoint 5: NonRetryableError -> force_dead -> immediate dead."""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from typing import Any

from engine import db, jobs
from engine.config import Settings
from engine.worker import HandlerContext, Worker


def _fast(settings: Settings) -> Settings:
    return replace(
        settings,
        backoff_base_seconds=0.05,
        backoff_cap_seconds=0.5,
        idle_sleep_min_seconds=0.02,
        idle_sleep_max_seconds=0.05,
    )


def _run_worker(settings: Settings, handlers: dict[str, Any]) -> tuple[Worker, threading.Thread]:
    worker = Worker(settings, handlers, application_name="engine-worker-test")
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    return worker, thread


def _wait_until(predicate: Any, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition not reached in time")


def test_nonretryable_error_sends_job_straight_to_dead(clean_db: Settings) -> None:
    settings = _fast(clean_db)
    conn = db.connect(settings)

    def bad(payload: dict[str, Any], ctx: HandlerContext) -> dict[str, Any]:
        raise jobs.NonRetryableError("configuration missing")

    job = jobs.enqueue(
        conn, job_type="boom", payload={"x": 1}, max_attempts=5
    )
    worker, thread = _run_worker(settings, {"boom": bad})
    try:
        _wait_until(lambda: _state(conn, job.id) == "dead", timeout=15)
    finally:
        _stop(worker, thread)

    row = _job(conn, job.id)
    assert row["attempts"] == 1, "no second attempt may happen"
    assert row["state"] == "dead"
    assert "non-retryable: configuration missing" in row["last_error"]
    assert row["result"] is None


def test_retryable_error_still_uses_the_budget(clean_db: Settings) -> None:
    settings = _fast(clean_db)
    conn = db.connect(settings)

    def bad(payload: dict[str, Any], ctx: HandlerContext) -> dict[str, Any]:
        raise RuntimeError("transient")

    job = jobs.enqueue(
        conn, job_type="boom", payload={"x": 1}, max_attempts=3
    )
    worker, thread = _run_worker(settings, {"boom": bad})
    try:
        _wait_until(lambda: _state(conn, job.id) == "dead", timeout=30)
    finally:
        _stop(worker, thread)

    row = _job(conn, job.id)
    assert row["attempts"] == 3, "a plain error still consumes the budget"
    assert row["state"] == "dead"


def test_force_dead_reaches_the_fail_sql_case(clean_db: Settings) -> None:
    """fail(force_dead=True) deads a running job in one statement."""
    conn = db.connect(clean_db)
    job = jobs.enqueue(conn, job_type="plain")
    claimed = jobs.claim(
        conn, locked_by="w", lease_seconds=30.0, types=["plain"]
    )
    assert claimed is not None and claimed.state == "running"
    failed = jobs.fail(
        conn,
        job_id=job.id,
        locked_by="w",
        attempts=claimed.attempts,
        backoff_seconds=0.0,
        last_error="unsalvageable",
        force_dead=True,
    )
    assert failed.state == "dead"
    assert failed.attempts == 1, "attempts is untouched by the dead decision"
    assert failed.max_attempts == clean_db.max_attempts, (
        "force_dead beats the budget, whatever it was"
    )


def _state(conn: Any, job_id: Any) -> Any:
    return _job(conn, job_id)["state"]


def _job(conn: Any, job_id: Any) -> Any:
    row = conn.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()
    assert row is not None
    return row


def _stop(worker: Worker, thread: threading.Thread) -> None:
    worker.request_stop()
    thread.join(timeout=10)
    assert not thread.is_alive()
