"""Checkpoint 2: heartbeat, fencing, complete, fail."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import psycopg
import pytest

from engine import db, jobs
from engine.config import Settings
from engine.worker import HeartbeatThread


def _requeue_like_a_reaper(conn: Any, job_id: Any) -> None:
    """Stand-in for the Phase 1 reaper: send a running job back to queued."""
    conn.execute(
        "UPDATE jobs SET state = 'queued', locked_by = NULL, lease_expires_at = NULL,"
        " last_heartbeat_at = NULL, updated_at = now() WHERE id = %(job_id)s",
        {"job_id": job_id},
    )


def _settings_with_lease(settings: Settings, lease: float) -> Settings:
    """Replace the lease and its derived heartbeat interval together."""
    return replace(settings, lease_seconds=lease, heartbeat_interval_seconds=lease / 3)


def _claim_when_ready(conn: Any, worker: str, settings: Settings) -> jobs.Job | None:
    """Claim, waiting until the job's run_at passes (jittered backoff)."""
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        job = _claim(conn, worker, settings)
        if job is not None:
            return job
        time.sleep(0.01)
    return None


def _claim(
    conn: Any, worker: str, settings: Settings
) -> jobs.Job | None:
    return jobs.claim(
        conn,
        locked_by=worker,
        lease_seconds=settings.lease_seconds,
        types=["demo"],
    )


def test_heartbeat_extends_the_lease(clean_db: Settings) -> None:
    conn = db.connect(clean_db)
    jobs.enqueue(conn, job_type="demo", payload={})
    job = _claim(conn, "worker-a", clean_db)
    assert job is not None
    original = job.lease_expires_at
    assert original is not None

    time.sleep(0.05)
    beaten = jobs.heartbeat(
        conn,
        job_id=job.id,
        locked_by="worker-a",
        attempts=job.attempts,
        lease_seconds=clean_db.lease_seconds,
    )
    assert beaten.state == "running"
    assert beaten.lease_expires_at is not None and beaten.lease_expires_at > original


def test_heartbeat_after_reclaim_raises_lease_lost(clean_db: Settings) -> None:
    conn = db.connect(clean_db)
    jobs.enqueue(conn, job_type="demo", payload={})
    a = _claim(conn, "worker-a", clean_db)
    assert a is not None and a.attempts == 1

    _requeue_like_a_reaper(conn, a.id)
    b = _claim(conn, "worker-b", clean_db)
    assert b is not None and b.attempts == 2

    with pytest.raises(jobs.LeaseLost):
        jobs.heartbeat(
            conn,
            job_id=a.id,
            locked_by="worker-a",
            attempts=a.attempts,
            lease_seconds=clean_db.lease_seconds,
        )


def test_stale_complete_fails_and_result_belongs_to_new_attempt(clean_db: Settings) -> None:
    conn = db.connect(clean_db)
    jobs.enqueue(conn, job_type="demo", payload={})
    a = _claim(conn, "worker-a", clean_db)
    assert a is not None and a.attempts == 1

    _requeue_like_a_reaper(conn, a.id)
    b = _claim(conn, "worker-b", clean_db)
    assert b is not None and b.attempts == 2

    finished = jobs.complete(
        conn, job_id=b.id, locked_by="worker-b", attempts=b.attempts, result={"who": "b"}
    )
    assert finished.state == "succeeded"

    with pytest.raises(jobs.LeaseLost):
        jobs.complete(
            conn, job_id=a.id, locked_by="worker-a", attempts=a.attempts, result={"who": "a"}
        )

    row = conn.execute(
        "SELECT state, result, attempts FROM jobs WHERE id = %(id)s", {"id": b.id}
    ).fetchone()
    assert row is not None
    assert row["state"] == "succeeded"
    assert row["result"] == {"who": "b"}, "the stale worker must not overwrite B's result"
    assert row["attempts"] == 2


def test_stale_fail_raises_lease_lost_and_cannot_requeue_a_finished_job(
    clean_db: Settings,
) -> None:
    conn = db.connect(clean_db)
    jobs.enqueue(conn, job_type="demo", payload={})
    a = _claim(conn, "worker-a", clean_db)
    assert a is not None and a.attempts == 1

    _requeue_like_a_reaper(conn, a.id)
    b = _claim(conn, "worker-b", clean_db)
    assert b is not None and b.attempts == 2

    finished = jobs.complete(
        conn, job_id=b.id, locked_by="worker-b", attempts=b.attempts, result={"who": "b"}
    )
    assert finished.state == "succeeded"

    with pytest.raises(jobs.LeaseLost):
        jobs.fail(
            conn,
            job_id=a.id,
            locked_by="worker-a",
            attempts=a.attempts,
            backoff_seconds=0.5,
            last_error="stale fail",
        )

    row = conn.execute(
        "SELECT state, result, attempts FROM jobs WHERE id = %(id)s", {"id": a.id}
    ).fetchone()
    assert row is not None
    assert row["state"] == "succeeded", "a stale fail must not requeue a finished job"
    assert row["result"] == {"who": "b"}
    assert row["attempts"] == 2


def test_retries_show_growing_run_at_delays(clean_db: Settings) -> None:
    settings = replace(clean_db, backoff_base_seconds=0.1)
    conn = db.connect(settings)
    jobs.enqueue(conn, job_type="demo", payload={}, max_attempts=settings.max_attempts)

    delays: list[float] = []
    for attempt in range(1, 4):
        # The pure function carries the invariant: with the injectable jitter
        # pinned to its upper bound, backoff == min(cap, base * 2^attempts).
        backoff = jobs.jittered_backoff_seconds(
            attempt, settings, jitter=lambda low, high: high
        )
        assert backoff == pytest.approx(jobs.backoff_bound_seconds(attempt, settings))
        delays.append(backoff)

        job = _claim_when_ready(conn, "worker-a", settings)
        assert job is not None and job.attempts == attempt
        db_now_row = conn.execute("SELECT now() AS ts").fetchone()
        assert db_now_row is not None
        db_now = db_now_row["ts"]
        failed = jobs.fail(
            conn,
            job_id=job.id,
            locked_by="worker-a",
            attempts=job.attempts,
            backoff_seconds=backoff,
            last_error="boom",
        )
        assert failed.state == "queued"
        delay = (failed.run_at - db_now).total_seconds()
        # loose database-side sanity check: the gap between db_now and the
        # statement's now() is scheduling noise, not part of the contract.
        assert 0 <= delay <= backoff + 0.5

    assert delays == sorted(delays) and len(set(delays)) == len(delays), (
        f"retries must grow: {delays}"
    )


def test_job_goes_dead_at_max_attempts(clean_db: Settings) -> None:
    settings = replace(clean_db, backoff_base_seconds=0.001, max_attempts=3)
    conn = db.connect(settings)
    jobs.enqueue(conn, job_type="demo", payload={}, max_attempts=3)

    states: list[str] = []
    run_at_before_final_fail: Any = None
    failed: jobs.Job | None = None
    for attempt in range(1, 4):
        job = _claim_when_ready(conn, "worker-a", settings)
        assert job is not None and job.attempts == attempt
        failed = jobs.fail(
            conn,
            job_id=job.id,
            locked_by="worker-a",
            attempts=job.attempts,
            backoff_seconds=jobs.jittered_backoff_seconds(job.attempts, settings),
            last_error=f"boom {attempt}",
        )
        states.append(failed.state)
        if attempt == 2:
            run_at_before_final_fail = failed.run_at

    assert states == ["queued", "queued", "dead"]
    assert failed is not None
    assert failed.attempts == 3
    assert failed.last_error == "boom 3"
    assert failed.locked_by is None and failed.lease_expires_at is None
    assert failed.run_at == run_at_before_final_fail, "a dead job gets no new run_at"
    assert _claim(conn, "worker-a", settings) is None, "dead jobs are not claimable"


def _wait_until(predicate: Callable[[], Any], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition not reached in time")


def test_heartbeat_thread_extends_the_lease(clean_db: Settings) -> None:
    conn = db.connect(clean_db)
    jobs.enqueue(conn, job_type="demo", payload={})
    job = _claim(conn, "worker-a", clean_db)
    assert job is not None
    original = job.lease_expires_at
    assert original is not None

    heart = HeartbeatThread(clean_db, job)
    heart.start()
    try:
        def _extended() -> bool:
            row = conn.execute(
                "SELECT lease_expires_at FROM jobs WHERE id = %(id)s", {"id": job.id}
            ).fetchone()
            return bool(
                row is not None
                and row["lease_expires_at"] is not None
                and row["lease_expires_at"] > original
            )

        _wait_until(_extended)
    finally:
        heart.stop()
        heart.join(timeout=5)
    assert not heart.lease_lost


def test_heartbeat_thread_detects_a_lost_lease(clean_db: Settings) -> None:
    conn = db.connect(clean_db)
    settings = _settings_with_lease(clean_db, 0.9)  # interval = 0.3s
    jobs.enqueue(conn, job_type="demo", payload={})
    job = _claim(conn, "worker-a", settings)
    assert job is not None

    heart = HeartbeatThread(settings, job)
    heart.start()
    try:
        time.sleep(settings.heartbeat_interval_seconds + 0.1)  # let it beat once
        _requeue_like_a_reaper(conn, job.id)  # lease is now lost
        _wait_until(lambda: heart.lease_lost)
    finally:
        heart.stop()
        heart.join(timeout=5)
    assert heart.lease_lost


def test_heartbeat_survives_a_terminated_backend(clean_db: Settings) -> None:
    conn = db.connect(clean_db)
    jobs.enqueue(conn, job_type="demo", payload={})
    job = _claim(conn, "worker-a", clean_db)
    assert job is not None
    original = job.lease_expires_at
    assert original is not None

    heart = HeartbeatThread(clean_db, job)
    heart.start()
    try:
        _wait_until(lambda: _lease_of(conn, job.id) > original)  # first beat

        lease_before_kill = _lease_of(conn, job.id)
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
            " WHERE application_name = 'engine-heartbeat'"
        )
        # transient error: the thread must reconnect, beat again, and never
        # set lease_lost over a mere connection drop.
        _wait_until(lambda: _lease_of(conn, job.id) > lease_before_kill)
        assert not heart.lease_lost
        assert heart.is_alive()
    finally:
        heart.stop()
        heart.join(timeout=5)
    assert not heart.lease_lost


def _lease_of(conn: Any, job_id: Any) -> Any:
    row = conn.execute(
        "SELECT lease_expires_at FROM jobs WHERE id = %(id)s", {"id": job_id}
    ).fetchone()
    assert row is not None
    return row["lease_expires_at"]


def test_complete_rejects_jsonb_hostile_values(clean_db: Settings) -> None:
    """Postgres jsonb rejects NaN and \\u0000; complete must raise DataError.

    json.dumps accepts both by default, so only the database catches them —
    the worker's catch depends on the rejection being a DataError (SQLSTATE
    class 22), which this pins. Under autocommit the failed statement leaves
    the job running with its token intact and the connection usable.
    """
    conn = db.connect(clean_db)
    jobs.enqueue(conn, job_type="demo", payload={})
    job = jobs.claim(conn, locked_by="worker-a", lease_seconds=30.0, types=["demo"])
    assert job is not None

    with pytest.raises(psycopg.DataError):  # NaN: invalid json token
        jobs.complete(
            conn,
            job_id=job.id,
            locked_by="worker-a",
            attempts=job.attempts,
            result={"x": float("nan")},
        )
    with pytest.raises(psycopg.DataError):  # \u0000: untranslatable escape
        jobs.complete(
            conn,
            job_id=job.id,
            locked_by="worker-a",
            attempts=job.attempts,
            result={"s": "bad\u0000nul"},
        )

    row = conn.execute(
        "SELECT state FROM jobs WHERE id = %(id)s", {"id": job.id}
    ).fetchone()
    assert row is not None and row["state"] == "running"
    # the connection survived both rejections (autocommit discards only the
    # failed statement) — the worker may keep using it to record the failure.
    finished = jobs.complete(
        conn,
        job_id=job.id,
        locked_by="worker-a",
        attempts=job.attempts,
        result={"ok": True},
    )
    assert finished.state == "succeeded"
