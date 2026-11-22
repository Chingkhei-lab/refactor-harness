"""Persistence for agent runs and model calls (ADR 006 D2–D4).

One row per `(run_id, turn)`, `UNIQUE` in the schema, `ON CONFLICT DO NOTHING` on
insert: a redelivered turn writes nothing rather than overwriting a committed
response with a fresh one. That is the whole idempotency story, and it lives in the
database because an application-level "already did I call?" is exactly the check
that loses a race.

`append` takes an optional connection so the turn can be committed inside the same
transaction as the workflow step's advance — the caller owns the commit. Without a
connection it commits on its own, which is what the tests and the standalone loop
use.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg

from .prompts import PROMPT_VERSION
from .types import AssistantTurn, Budget, ModelRequest, Outcome, canonical_json

_SQL_DIR = Path(__file__).parent / "sql"
_RUN_INSERT = (_SQL_DIR / "agent_run_insert.sql").read_text(encoding="utf-8")
_CALL_INSERT = (_SQL_DIR / "llm_call_insert.sql").read_text(encoding="utf-8")
_CALLS_LOAD = (_SQL_DIR / "llm_calls_load.sql").read_text(encoding="utf-8")
_RUN_GET = (_SQL_DIR / "agent_run_get.sql").read_text(encoding="utf-8")


class DivergenceError(RuntimeError):
    """A committed turn cannot be replayed for the request built now.

    Raised instead of reusing the stored response. The run is a different
    experiment than the one that produced those turns, and continuing would write
    a log that never happened (ADR 006 D3).
    """


@dataclass(frozen=True)
class CommittedTurn:
    turn: int
    request_sha256: str
    assistant: AssistantTurn
    status: str
    error: str | None


class CallStore:
    def __init__(self, database_url: str) -> None:
        self._url = database_url

    @contextmanager
    def _connection(self, conn: psycopg.Connection[Any] | None) -> Iterator[Any]:
        if conn is not None:
            yield conn
            return
        with psycopg.connect(self._url) as owned:
            yield owned

    def start_run(
        self,
        *,
        run_id: str,
        task_id: str,
        provider: str,
        model: str,
        prompt_sha256: str,
        temperature: float,
        budget: Budget,
        conn: psycopg.Connection[Any] | None = None,
    ) -> None:
        with self._connection(conn) as connection:
            connection.execute(
                _RUN_INSERT,
                {
                    "id": UUID(run_id),
                    "task_id": task_id,
                    "provider": provider,
                    "model": model,
                    "prompt_version": PROMPT_VERSION,
                    "prompt_sha256": prompt_sha256,
                    "temperature": temperature,
                    "budget": canonical_json(budget.to_json()),
                    "status": "running",
                    "outcome": None,
                    "reason": None,
                },
            )

    def append(
        self,
        *,
        run_id: str,
        turn: int,
        request_sha256: str,
        assistant: AssistantTurn | None,
        provider: str,
        model: str,
        status: str = "ok",
        error: str | None = None,
        conn: psycopg.Connection[Any] | None = None,
    ) -> bool:
        """Commit one turn. False means it was already committed (no overwrite)."""
        payload = canonical_json(assistant.to_json() if assistant is not None else {})
        with self._connection(conn) as connection:
            row = connection.execute(
                _CALL_INSERT,
                {
                    "run_id": UUID(run_id),
                    "turn": turn,
                    "request_sha256": request_sha256,
                    "response": payload,
                    "provider": provider,
                    "model": model,
                    "tokens_prompt": assistant.tokens_prompt if assistant else None,
                    "tokens_completion": assistant.tokens_completion if assistant else None,
                    "latency_ms": assistant.latency_ms if assistant else None,
                    "attempts": assistant.attempts if assistant else 1,
                    "status": status,
                    "error": error,
                },
            ).fetchone()
        return row is not None

    def committed(self, run_id: str) -> list[CommittedTurn]:
        with self._connection(None) as connection:
            rows = connection.execute(_CALLS_LOAD, {"run_id": UUID(run_id)}).fetchall()
        turns: list[CommittedTurn] = []
        for row in rows:
            (turn, request_sha256, response, provider, model, status, error) = row
            payload: Mapping[str, Any] = response
            turns.append(
                CommittedTurn(
                    turn=int(turn),
                    request_sha256=str(request_sha256),
                    assistant=AssistantTurn.from_json(payload if isinstance(payload, dict) else {}),
                    status=str(status),
                    error=error,
                )
            )
        return turns

    def finish(
        self,
        *,
        run_id: str,
        task_id: str,
        provider: str,
        model: str,
        prompt_sha256: str,
        temperature: float,
        budget: Budget,
        status: str,
        outcome: Outcome,
        reason: str,
        conn: psycopg.Connection[Any] | None = None,
    ) -> None:
        with self._connection(conn) as connection:
            connection.execute(
                _RUN_INSERT,
                {
                    "id": UUID(run_id),
                    "task_id": task_id,
                    "provider": provider,
                    "model": model,
                    "prompt_version": PROMPT_VERSION,
                    "prompt_sha256": prompt_sha256,
                    "temperature": temperature,
                    "budget": canonical_json(budget.to_json()),
                    "status": status,
                    "outcome": outcome.value,
                    "reason": reason,
                },
            )

    def run(self, run_id: str) -> Mapping[str, Any] | None:
        with self._connection(None) as connection, connection.cursor() as cursor:
            cursor.execute(_RUN_GET, {"id": UUID(run_id)})
            row = cursor.fetchone()
            if row is None:
                return None
            columns = [column.name for column in cursor.description or ()]
        return dict(zip(columns, row, strict=True))


def encode_request(request: ModelRequest) -> str:
    """Kept separate so a request and its hash cannot drift apart."""
    return request.sha256()


def load_response_json(text: str) -> AssistantTurn:
    return AssistantTurn.from_json(json.loads(text))
