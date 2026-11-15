"""Reaper tests: requeue/dead/live-lease, and two reapers racing."""

from __future__ import annotations

import threading
import time
from typing import Any

from engine import db, jobs
from engine.config import Settings


def _claim_expired(conn: Any, worker: str, settings: Settings) -> jobs.Job | None:
    return jobs.claim(
        conn,
        locked_by=worker,
        lease_seconds=0.05,  # expires almost immediately
        types=["demo"],
    )


def _row(conn: Any, job_id: Any) -> Any:
    row = conn.execute("SELECT * FROM jobs WHERE id = %(id)s", {"id": job_id}).fetchone()
    assert row is not None
    return row


def test_reaper_requeues_dead_and_leaves_live_leases(clean_db: Settings) -> None:
    conn = db.connect(clean_db)
    jobs.enqueue(conn, job_type="demo", payload={"i": 1}, max_attempts=5)
    requeueable = _claim_expired(conn, "worker-a", clean_db)
    jobs.enqueue(conn, job_type="demo", payload={"i": 2}, max_attempts=1)
    dead_candidate = _claim_expired(conn, "worker-a", clean_db)
    jobs.enqueue(conn, job_type="demo", payload={"i": 3})
    live = jobs.claim(
        conn, locked_by="worker-a", lease_seconds=30.0, types=["demo"]
    )
    assert requeueable is not None and dead_candidate is not None and live is not None
    assert requeueable.attempts == 1 and dead_candidate.attempts == 1

    time.sleep(0.15)  # let both short leases expire
    reaped = jobs.reaper(conn, batch=10)
    assert {job.id for job in reaped} == {requeueable.id, dead_candidate.id}

    row = _row(conn, requeueable.id)
    assert row["state"] == "queued"
    assert row["last_error"] == "lease expired"
    assert row["attempts"] == 1, "the reaper must not increment attempts"
    assert row["locked_by"] is None and row["lease_expires_at"] is None

    dead_row = _row(conn, dead_candidate.id)
    assert dead_row["state"] == "dead"
    assert dead_row["attempts"] == 1 and dead_row["max_attempts"] == 1
    assert dead_row["last_error"] == "lease expired"

    live_row = _row(conn, live.id)
    assert live_row["state"] == "running", "a live lease must be left alone"
    assert live_row["locked_by"] == "worker-a"
    assert live_row["last_error"] is None


def test_two_reapers_process_each_expired_job_once(clean_db: Settings) -> None:
    settings = clean_db
    conn = db.connect(settings)
    total = 20
    for i in range(total):
        jobs.enqueue(conn, job_type="demo", payload={"i": i})
    for _ in range(total):
        assert _claim_expired(conn, "worker-a", settings) is not None
    time.sleep(0.15)  # all leases expired

    reaped_ids: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def reap() -> None:
        worker_conn = db.connect(settings)
        barrier.wait()
        for _ in range(50):
            rows = jobs.reaper(worker_conn, batch=3)
            if not rows:
                break
            with lock:
                reaped_ids.extend(str(job.id) for job in rows)

    threads = [threading.Thread(target=reap) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(reaped_ids) == total, f"expected {total}, got {len(reaped_ids)}"
    assert len(set(reaped_ids)) == total, "no job may be reaped twice"
