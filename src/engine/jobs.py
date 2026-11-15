"""Queue operations. The SQL lives in src/engine/sql/ and is the point."""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Json

from .config import Settings, load_settings
from .db import Row

_SQL_DIR = Path(__file__).parent / "sql"


def _load_sql(name: str) -> str:
    return (_SQL_DIR / name).read_text(encoding="utf-8")


_ENQUEUE_SQL = _load_sql("enqueue.sql")
_CLAIM_SQL = _load_sql("claim.sql")
_HEARTBEAT_SQL = _load_sql("heartbeat.sql")
_COMPLETE_SQL = _load_sql("complete.sql")
_FAIL_SQL = _load_sql("fail.sql")
_REAPER_SQL = _load_sql("reaper.sql")

_RETURNING_COLUMNS = (
    "id, type, payload, state, attempts, max_attempts, run_at, locked_by, "
    "lease_expires_at, last_heartbeat_at, result, last_error, dedupe_key, "
    "created_at, updated_at"
)


class LeaseLost(Exception):
    """A fenced write matched zero rows: the lease is gone. Never swallow it."""


@dataclass(frozen=True)
class Job:
    id: UUID
    type: str
    payload: dict[str, Any]
    state: str
    attempts: int
    max_attempts: int
    run_at: datetime
    locked_by: str | None
    lease_expires_at: datetime | None
    last_heartbeat_at: datetime | None
    result: dict[str, Any] | None
    last_error: str | None
    dedupe_key: str | None
    created_at: datetime
    updated_at: datetime


def _to_job(row: Row) -> Job:
    return Job(
        id=row["id"],
        type=row["type"],
        payload=row["payload"],
        state=row["state"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        run_at=row["run_at"],
        locked_by=row["locked_by"],
        lease_expires_at=row["lease_expires_at"],
        last_heartbeat_at=row["last_heartbeat_at"],
        result=row["result"],
        last_error=row["last_error"],
        dedupe_key=row["dedupe_key"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _fenced_write(
    conn: psycopg.Connection[Row],
    sql: str,
    params: dict[str, Any],
) -> Job:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        raise LeaseLost("fenced write matched zero rows: the lease was lost")
    return _to_job(row)


def enqueue(
    conn: psycopg.Connection[Row],
    *,
    job_type: str,
    payload: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
    run_at: datetime | None = None,
    delay_seconds: float | None = None,
    max_attempts: int | None = None,
    settings: Settings | None = None,
) -> Job:
    """Insert a job, or return the existing job for a duplicate dedupe_key.

    `run_at` schedules absolutely; `delay_seconds` schedules relative to the
    database's now() (run_at = now() + delay). run_at wins when both are set.
    """
    settings = settings or load_settings()
    row = conn.execute(
        _ENQUEUE_SQL,
        {
            "job_type": job_type,
            "payload": Json(payload) if payload is not None else Json({}),
            "max_attempts": max_attempts if max_attempts is not None else settings.max_attempts,
            "run_at": run_at,
            "delay_seconds": delay_seconds,
            "dedupe_key": dedupe_key,
        },
    ).fetchone()
    if row is None:
        raise RuntimeError("enqueue did not return a row")
    return _to_job(row)


def claim(
    conn: psycopg.Connection[Row],
    *,
    locked_by: str,
    lease_seconds: float,
    types: list[str],
) -> Job | None:
    """Claim the next eligible job, or return None when nothing is eligible.

    Single statement on an autocommit connection: select with SKIP LOCKED and
    flip to running in one shot, so nothing is held while a handler runs.
    `types` limits claims to job types this worker has handlers for.
    """
    row = conn.execute(
        _CLAIM_SQL,
        {"locked_by": locked_by, "lease_seconds": lease_seconds, "types": types},
    ).fetchone()
    if row is None:
        return None
    return _to_job(row)


def heartbeat(
    conn: psycopg.Connection[Row],
    *,
    job_id: UUID,
    locked_by: str,
    attempts: int,
    lease_seconds: float,
) -> Job:
    """Extend the lease, fenced by the full claim token."""
    return _fenced_write(
        conn,
        _HEARTBEAT_SQL,
        {
            "job_id": job_id,
            "locked_by": locked_by,
            "attempts": attempts,
            "lease_seconds": lease_seconds,
        },
    )


def complete(
    conn: psycopg.Connection[Row],
    *,
    job_id: UUID,
    locked_by: str,
    attempts: int,
    result: dict[str, Any] | None,
) -> Job:
    """Mark succeeded and store the result, fenced by the full claim token."""
    return _fenced_write(
        conn,
        _COMPLETE_SQL,
        {
            "job_id": job_id,
            "locked_by": locked_by,
            "attempts": attempts,
            "result": Json(result) if result is not None else None,
        },
    )


def fail(
    conn: psycopg.Connection[Row],
    *,
    job_id: UUID,
    locked_by: str,
    attempts: int,
    backoff_seconds: float,
    last_error: str,
) -> Job:
    """Requeue with backoff, or go dead at max_attempts, fenced by the token."""
    return _fenced_write(
        conn,
        _FAIL_SQL,
        {
            "job_id": job_id,
            "locked_by": locked_by,
            "attempts": attempts,
            "backoff_seconds": backoff_seconds,
            "last_error": last_error,
        },
    )


def reaper(
    conn: psycopg.Connection[Row],
    *,
    batch: int,
) -> list[Job]:
    """Reclaim expired leases: requeue or dead, never increment attempts.

    Single statement, safe on an autocommit connection. Returns the affected
    rows so callers can count them.
    """
    rows = conn.execute(_REAPER_SQL, {"batch": batch}).fetchall()
    return [_to_job(row) for row in rows]


def backoff_bound_seconds(attempts: int, settings: Settings) -> float:
    """Upper bound of the full-jitter backoff: min(cap, base * 2^attempts)."""
    delay: float = settings.backoff_base_seconds * (2**attempts)
    if delay > settings.backoff_cap_seconds:
        return settings.backoff_cap_seconds
    return delay


def jittered_backoff_seconds(
    attempts: int,
    settings: Settings,
    jitter: Callable[[float, float], float] | None = None,
) -> float:
    """Full jitter: uniform random between 0 and the backoff bound.

    `jitter` is injectable so tests can pin it without monkeypatching the
    global random module; the default is random.uniform.
    """
    bound = backoff_bound_seconds(attempts, settings)
    uniform = jitter if jitter is not None else random.uniform
    return float(uniform(0.0, bound))
