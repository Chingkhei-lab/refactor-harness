"""Dead-letter listing and replay (checkpoint 5).

`python -m engine.dlq list [--limit N]` inspects the dead_letters view;
`python -m engine.dlq replay <job_id> [--budget N]` requeues one dead job.

Replay runs in one transaction in the global lock order job -> step ->
workflow (ADR 002): lock the job WHERE it is dead (refuse otherwise), state
-> queued with `run_at = now()` and a fresh budget `max_attempts = attempts +
budget` (attempts itself is never touched — claim owns it), replay_count + 1;
then, for workflow steps, the step back to pending and the workflow back to
running with failed_step cleared. `DeadlockDetected` (e.g. racing a step
completion) retries the whole transaction exactly once.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg

from . import db
from .config import load_settings

logger = logging.getLogger(__name__)

_SQL_DIR = Path(__file__).parent / "sql"


def _load_sql(name: str) -> str:
    return (_SQL_DIR / name).read_text(encoding="utf-8")


_LIST_SQL = _load_sql("dlq_list.sql")
_LOCK_DEAD_JOB_SQL = _load_sql("dlq_lock_dead_job.sql")
_REPLAY_JOB_SQL = _load_sql("dlq_replay_job.sql")
_REPLAY_STEP_SQL = _load_sql("dlq_replay_step.sql")
_REPLAY_WORKFLOW_SQL = _load_sql("dlq_replay_workflow.sql")


class NotReplayable(Exception):
    """The job is missing or not dead: replay refuses to touch it."""


@dataclass(frozen=True)
class DeadLetter:
    id: UUID
    type: str
    workflow_id: UUID | None
    workflow_type: str | None
    step_position: int | None
    step_name: str | None
    workflow_state: str | None
    attempts: int
    max_attempts: int
    replay_count: int
    last_error: str | None
    updated_at: datetime


@dataclass(frozen=True)
class ReplayResult:
    job_id: UUID
    state: str
    attempts: int
    max_attempts: int
    replay_count: int
    workflow_restarted: bool


def list_dead(
    conn: psycopg.Connection[db.Row], *, limit: int = 20
) -> list[DeadLetter]:
    """Read the dead_letters view, newest first."""
    rows = conn.execute(_LIST_SQL, {"limit": limit}).fetchall()
    return [
        DeadLetter(
            id=row["id"],
            type=row["type"],
            workflow_id=row["workflow_id"],
            workflow_type=row["workflow_type"],
            step_position=row["step_position"],
            step_name=row["step_name"],
            workflow_state=row["workflow_state"],
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            replay_count=row["replay_count"],
            last_error=row["last_error"],
            updated_at=row["updated_at"],
        )
        for row in rows
    ]


def replay(
    conn: psycopg.Connection[db.Row],
    job_id: UUID,
    *,
    budget: int,
) -> ReplayResult:
    """Requeue one dead job with a fresh retry budget; retry once on deadlock."""
    if budget < 1:
        raise ValueError(f"budget must be >= 1, got {budget}")
    try:
        return _replay_once(conn, job_id, budget)
    except psycopg.errors.DeadlockDetected:
        # the transaction rolled back with the context manager; one clean
        # retry, then the second deadlock propagates to the caller.
        logger.warning("replay of job %s deadlocked once; retrying", job_id)
        return _replay_once(conn, job_id, budget)


def _replay_once(
    conn: psycopg.Connection[db.Row], job_id: UUID, budget: int
) -> ReplayResult:
    from .workflows import WORKFLOW_JOB_TYPE  # local: workflows imports worker

    with conn.transaction():
        row = conn.execute(_LOCK_DEAD_JOB_SQL, {"job_id": job_id}).fetchone()
        if row is None:
            raise NotReplayable(f"job {job_id} not found")
        if row["state"] != "dead":
            raise NotReplayable(f"job {job_id} is {row['state']!r}, not 'dead'")

        updated = conn.execute(
            _REPLAY_JOB_SQL, {"job_id": job_id, "budget": budget}
        ).fetchone()
        if updated is None:  # unreachable while the row lock is held
            raise NotReplayable(f"job {job_id} was not dead at update time")

        workflow_restarted = False
        payload: dict[str, Any] = row["payload"] or {}
        if (
            row["type"] == WORKFLOW_JOB_TYPE
            and payload.get("workflow_id")
            and payload.get("position") is not None
        ):
            workflow_id = UUID(str(payload["workflow_id"]))
            position = int(payload["position"])
            conn.execute(
                _REPLAY_STEP_SQL,
                {"workflow_id": workflow_id, "position": position},
            )
            conn.execute(_REPLAY_WORKFLOW_SQL, {"workflow_id": workflow_id})
            workflow_restarted = True

        return ReplayResult(
            job_id=updated["id"],
            state=updated["state"],
            attempts=updated["attempts"],
            max_attempts=updated["max_attempts"],
            replay_count=updated["replay_count"],
            workflow_restarted=workflow_restarted,
        )


def _format(letter: DeadLetter) -> str:
    wf = (
        "-"
        if letter.workflow_id is None
        else f"{letter.workflow_type}/{str(letter.workflow_id)[:8]}"
    )
    step = (
        "-"
        if letter.step_name is None
        else f"{letter.step_name}[{letter.step_position}]"
    )
    error = (letter.last_error or "").replace("\n", " ")[:160]
    return (
        f"{letter.id} {letter.type} attempts={letter.attempts}/"
        f"{letter.max_attempts} replayed={letter.replay_count} wf={wf} "
        f"step={step} at={letter.updated_at:%Y-%m-%dT%H:%M:%S}\n    {error}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m engine.dlq", description="List and replay dead jobs."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    list_parser = sub.add_parser("list", help="list dead jobs, newest first")
    list_parser.add_argument("--limit", type=int, default=20)
    replay_parser = sub.add_parser(
        "replay", help="requeue one dead job with a fresh retry budget"
    )
    replay_parser.add_argument("job_id")
    replay_parser.add_argument(
        "--budget",
        type=int,
        default=None,
        help="attempts to add (default: the configured max_attempts)",
    )
    args = parser.parse_args(argv)

    settings = load_settings()
    conn = db.connect(settings, application_name="engine-dlq")
    try:
        if args.command == "list":
            letters = list_dead(conn, limit=args.limit)
            if not letters:
                print("no dead letters")
                return 0
            for letter in letters:
                print(_format(letter))
            return 0
        budget = (
            args.budget if args.budget is not None else settings.max_attempts
        )
        try:
            result = replay(conn, UUID(args.job_id), budget=budget)
        except NotReplayable as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1
        wf = (
            "workflow restarted"
            if result.workflow_restarted
            else "plain job"
        )
        print(
            f"replayed {result.job_id}: {result.state}, "
            f"max_attempts={result.max_attempts} "
            f"(attempts={result.attempts}, replay_count={result.replay_count}), "
            f"{wf}"
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
