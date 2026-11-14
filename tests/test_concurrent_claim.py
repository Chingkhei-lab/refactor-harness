"""Concurrent claim test: the foundation for everything else."""

from __future__ import annotations

import threading

from engine import db, jobs
from engine.config import Settings


def test_eight_workers_claim_each_job_exactly_once(clean_db: Settings) -> None:
    total = 1000
    conn = db.connect(clean_db)
    for i in range(total):
        jobs.enqueue(conn, job_type="demo", payload={"i": i})

    claimed: list[str] = []
    lock = threading.Lock()

    def run() -> None:
        worker_conn = db.connect(clean_db)
        local: list[str] = []
        while True:
            job = jobs.claim(
                worker_conn,
                locked_by=f"worker-{threading.get_ident()}",
                lease_seconds=clean_db.lease_seconds,
            )
            if job is None:
                break
            local.append(str(job.id))
        with lock:
            claimed.extend(local)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(claimed) == total, "every job must be claimed"
    assert len(set(claimed)) == total, "no job may be claimed twice"
