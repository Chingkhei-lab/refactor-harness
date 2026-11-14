"""Concurrent claim test: the foundation for everything else."""

from __future__ import annotations

import threading

from engine import db, jobs
from engine.config import Settings

WORKER_COUNT = 8
TOTAL_JOBS = 1000


def test_eight_workers_claim_each_job_exactly_once(clean_db: Settings) -> None:
    conn = db.connect(clean_db)
    for i in range(TOTAL_JOBS):
        jobs.enqueue(conn, job_type="demo", payload={"i": i})

    claimed: list[str] = []
    counts: dict[str, int] = {}
    lock = threading.Lock()
    barrier = threading.Barrier(WORKER_COUNT)

    def run(index: int) -> None:
        name = f"worker-{index}"
        worker_conn = db.connect(clean_db)
        local: list[str] = []
        barrier.wait()  # start together so the race window is real
        while True:
            job = jobs.claim(
                worker_conn,
                locked_by=name,
                lease_seconds=clean_db.lease_seconds,
                types=["demo"],
            )
            if job is None:
                break
            local.append(str(job.id))
        with lock:
            counts[name] = len(local)
            claimed.extend(local)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(WORKER_COUNT)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    print(f"jobs claimed per worker: {counts}")
    assert all(count > 0 for count in counts.values()), (
        "every worker must claim something, or nothing was tested"
    )
    assert len(claimed) == TOTAL_JOBS, "every job must be claimed"
    assert len(set(claimed)) == TOTAL_JOBS, "no job may be claimed twice"

    verify = db.connect(clean_db)
    row = verify.execute(
        "SELECT count(*) AS n FROM jobs WHERE attempts = 1 AND state = 'running'"
    ).fetchone()
    assert row is not None and row["n"] == TOTAL_JOBS, "each claim is exactly one attempt"
