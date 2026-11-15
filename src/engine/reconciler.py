"""Workflow repair loop: three rules, one global lock order (checkpoint 5).

Each rule runs its statements inside one transaction on the caller's
autocommit connection, taking row locks in the global order job -> step ->
workflow (ADR 002), so it cannot deadlock with the advance transaction or
DLQ replay. `DeadlockDetected` skips the rule — the transaction is rolled
back by the context manager and the next cycle retries it from scratch.

Rules (each one separate so a failure in one leaves the others untouched):

1. Dead current step -> step failed + workflow failed (statements, in lock
   order: lock the dead jobs, mark the steps failed, mark the workflows
   failed with `failed_step` + the job's `last_error`).
2. Current step pending with NO job row at all -> re-enqueue it with the
   snapshot's delay and retry budget. Not "no live job": a succeeded job row
   exists but is not live, and re-enqueueing it returns the same succeeded
   job forever — that state is rule 3's anomaly.
3. Succeeded job with a pending step -> workflow failed with an explanatory
   error, never re-enqueue (unreachable through the engine; surfacing only).
"""

from __future__ import annotations

from pathlib import Path

import psycopg

from . import db, jobs
from .config import Settings

_SQL_DIR = Path(__file__).parent / "sql"


def _load_sql(name: str) -> str:
    return (_SQL_DIR / name).read_text(encoding="utf-8")


_LOCK_DEAD_JOBS_SQL = _load_sql("reconciler_lock_dead_current_jobs.sql")
_FAIL_DEAD_STEPS_SQL = _load_sql("reconciler_fail_dead_steps.sql")
_FAIL_DEAD_WORKFLOWS_SQL = _load_sql("reconciler_fail_dead_workflows.sql")
_LOCK_MISSING_JOBS_SQL = _load_sql("reconciler_lock_missing_job_steps.sql")
_FAIL_INCONSISTENT_SQL = _load_sql("reconciler_fail_inconsistent_workflows.sql")


def reconcile(
    conn: psycopg.Connection[db.Row], *, settings: Settings
) -> dict[str, int]:
    """Run the three repair rules once; return rows touched per rule."""
    return {
        "dead_steps": _fail_dead_steps(conn, settings),
        "missing_jobs": _requeue_missing_jobs(conn, settings),
        "inconsistent": _fail_inconsistent(conn, settings),
    }


def _fail_dead_steps(
    conn: psycopg.Connection[db.Row], settings: Settings
) -> int:
    """Rule 1: dead current-step job -> step failed + workflow failed."""
    try:
        with conn.transaction():
            dead = conn.execute(
                _LOCK_DEAD_JOBS_SQL, {"batch": settings.reconciler_batch}
            ).fetchall()
            if not dead:
                return 0
            job_ids = [row["id"] for row in dead]
            conn.execute(_FAIL_DEAD_STEPS_SQL, {"job_ids": job_ids})
            failed = conn.execute(
                _FAIL_DEAD_WORKFLOWS_SQL, {"job_ids": job_ids}
            ).fetchall()
            return len(failed)
    except psycopg.errors.DeadlockDetected:
        return 0


def _requeue_missing_jobs(
    conn: psycopg.Connection[db.Row], settings: Settings
) -> int:
    """Rule 2: current step pending, no job row -> re-enqueue it.

    Local imports on purpose: workflows imports worker, and the worker
    imports this module; resolving it lazily keeps the import order simple.
    """
    from .workflows import WORKFLOW_JOB_TYPE, step_job_dedupe_key

    try:
        with conn.transaction():
            steps = conn.execute(
                _LOCK_MISSING_JOBS_SQL, {"batch": settings.reconciler_batch}
            ).fetchall()
            for row in steps:
                jobs.enqueue(
                    conn,
                    job_type=WORKFLOW_JOB_TYPE,
                    payload={
                        "workflow_id": str(row["workflow_id"]),
                        "position": row["position"],
                    },
                    dedupe_key=step_job_dedupe_key(row["workflow_id"], row["position"]),
                    delay_seconds=row["delay_seconds"],
                    max_attempts=row["max_attempts"],
                    settings=settings,
                )
            return len(steps)
    except psycopg.errors.DeadlockDetected:
        return 0


def _fail_inconsistent(
    conn: psycopg.Connection[db.Row], settings: Settings
) -> int:
    """Rule 3: succeeded job + pending step -> workflow failed, never requeue."""
    try:
        rows = conn.execute(
            _FAIL_INCONSISTENT_SQL, {"batch": settings.reconciler_batch}
        ).fetchall()
        return len(rows)
    except psycopg.errors.DeadlockDetected:
        return 0
