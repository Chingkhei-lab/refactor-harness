"""Handlers for the chaos script and the SIGTERM drain test."""

from __future__ import annotations

import os
import random
import socket
import sys
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (str(_REPO_ROOT / "src"),):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from engine import db  # noqa: E402
from engine.config import load_settings  # noqa: E402
from engine.worker import HandlerContext  # noqa: E402

_SIDE_EFFECT_SQL = """
INSERT INTO side_effects (job_id, attempt, worker)
VALUES (%(job_id)s, %(attempt)s, %(worker)s)
ON CONFLICT (job_id, attempt) DO NOTHING
RETURNING job_id
"""

_VIOLATION_SQL = """
INSERT INTO violations (job_id, attempt, worker, detail)
VALUES (%(job_id)s, %(attempt)s, %(worker)s, %(detail)s)
"""


def _connection() -> Any:
    return db.get_thread_connection(load_settings())


def _worker_label() -> str:
    return f"{socket.gethostname()}#{os.getpid()}"


def chaos_effect(payload: dict[str, Any], ctx: HandlerContext) -> dict[str, Any]:
    """Record the side effect, then sleep inside the kill window."""
    worker = _worker_label()
    inserted = _connection().execute(
        _SIDE_EFFECT_SQL,
        {"job_id": ctx.job_id, "attempt": ctx.attempt, "worker": worker},
    ).fetchone()
    if inserted is None:
        # the PRIMARY KEY fired: a second attempt ran the same job's effect
        _connection().execute(
            _VIOLATION_SQL,
            {
                "job_id": ctx.job_id,
                "attempt": ctx.attempt,
                "worker": worker,
                "detail": "duplicate side effect for (job_id, attempt)",
            },
        )
    time.sleep(random.uniform(0.005, 0.030))
    return {"ok": True}


def slow_handler(payload: dict[str, Any], ctx: HandlerContext) -> dict[str, Any]:
    """Long enough to observe a mid-handler SIGTERM drain."""
    time.sleep(2.0)
    return {"ok": True}


HANDLERS: dict[str, Any] = {"chaos": chaos_effect, "slow": slow_handler}
