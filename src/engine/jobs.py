"""Queue operations. The SQL lives in src/engine/sql/ and is the point."""

from __future__ import annotations

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

_COLUMNS = (
    "id, type, payload, state, attempts, max_attempts, run_at, locked_by, "
    "lease_expires_at, last_heartbeat_at, result, last_error, dedupe_key, "
    "created_at, updated_at"
)


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


def enqueue(
    conn: psycopg.Connection[Row],
    *,
    job_type: str,
    payload: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
    run_at: datetime | None = None,
    max_attempts: int | None = None,
    settings: Settings | None = None,
) -> Job:
    """Insert a job, or return the existing job for a duplicate dedupe_key."""
    settings = settings or load_settings()
    row = conn.execute(
        _ENQUEUE_SQL,
        {
            "job_type": job_type,
            "payload": Json(payload) if payload is not None else Json({}),
            "max_attempts": max_attempts if max_attempts is not None else settings.max_attempts,
            "run_at": run_at,
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
) -> Job | None:
    """Claim the next eligible job, or return None when the queue is empty.

    Single statement on an autocommit connection: select with SKIP LOCKED and
    flip to running in one shot, so nothing is held while a handler runs.
    """
    row = conn.execute(
        _CLAIM_SQL,
        {"locked_by": locked_by, "lease_seconds": lease_seconds},
    ).fetchone()
    if row is None:
        return None
    return _to_job(row)
