"""Connection management: one autocommit connection per thread.

Every operation is a single statement, so autocommit guarantees no transaction
can be left open around a handler. The heartbeat thread gets its own
connection; threads never share one.
"""

from __future__ import annotations

import threading
from typing import Any

import psycopg
from psycopg.rows import dict_row

from .config import Settings, load_settings

Row = dict[str, Any]

_local = threading.local()


def connect(
    settings: Settings | None = None,
    *,
    application_name: str | None = None,
) -> psycopg.Connection[Row]:
    """Open a new autocommit connection with row-level guards configured."""
    settings = settings or load_settings()
    kwargs: dict[str, Any] = {}
    if application_name is not None:
        kwargs["application_name"] = application_name
    return psycopg.connect(
        settings.database_url,
        autocommit=True,
        row_factory=dict_row,
        connect_timeout=settings.connect_timeout_seconds,
        options=f"-c statement_timeout={settings.statement_timeout_ms}",
        **kwargs,
    )


def get_thread_connection(
    settings: Settings,
    *,
    application_name: str | None = None,
) -> psycopg.Connection[Row]:
    """Return this thread's connection, opening one on first use.

    `settings` is required on purpose: defaulting to the environment made a
    handler silently target the wrong database when no DATABASE_URL was set.
    Callers pass the worker's settings explicitly.
    """
    conn: psycopg.Connection[Row] | None = getattr(_local, "conn", None)
    if conn is None or conn.closed:
        conn = connect(settings, application_name=application_name)
        _local.conn = conn
    return conn
