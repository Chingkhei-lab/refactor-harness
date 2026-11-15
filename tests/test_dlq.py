"""Checkpoint 5: the dead-letter view, replay, and the DLQ CLI."""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from engine import db, dlq, jobs, reconciler, workflows
from engine.config import Settings
from engine.worker import Worker
from engine.workflows import StepContext, StepDef, WorkflowDef


def _fast(settings: Settings) -> Settings:
    return replace(
        settings,
        backoff_base_seconds=0.05,
        backoff_cap_seconds=0.5,
        idle_sleep_min_seconds=0.02,
        idle_sleep_max_seconds=0.05,
    )


def _two_step(calls: dict[str, int]) -> WorkflowDef:
    def a(ctx: StepContext, prior: dict[str, Any]) -> dict[str, Any]:
        calls["a"] = calls.get("a", 0) + 1
        return {"a": 1}

    def b(ctx: StepContext, prior: dict[str, Any]) -> dict[str, Any]:
        calls["b"] = calls.get("b", 0) + 1
        return {"b": 2}

    return WorkflowDef(type="chain", steps=(StepDef("a", a), StepDef("b", b)))


def _make_dead_plain_job(conn: Any, dedupe: str) -> Any:
    job = jobs.enqueue(conn, job_type="plain", dedupe_key=dedupe)
    conn.execute(
        "UPDATE jobs SET state = 'dead', attempts = 2, last_error = 'boom'"
        " WHERE id = %(id)s",
        {"id": job.id},
    )
    return job.id


def _run_worker(settings: Settings, handlers: dict[str, Any]) -> tuple[Worker, threading.Thread]:
    worker = Worker(settings, handlers, application_name="engine-worker-test")
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    return worker, thread


def _stop(worker: Worker, thread: threading.Thread) -> None:
    worker.request_stop()
    thread.join(timeout=10)
    assert not thread.is_alive()


def _wait_until(predicate: Any, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition not reached in time")


def test_dead_letters_view_lists_plain_and_workflow_jobs(clean_db: Settings) -> None:
    settings = _fast(clean_db)
    conn = db.connect(settings)
    plain_id = _make_dead_plain_job(conn, "plain-dead")

    calls: dict[str, int] = {}
    wf = workflows.start_workflow(
        conn, wf_type="chain", definition=_two_step(calls)
    )
    step_job = conn.execute(
        "SELECT id FROM jobs WHERE dedupe_key = %(k)s",
        {"k": workflows.step_job_dedupe_key(wf.id, 0)},
    ).fetchone()
    assert step_job is not None
    conn.execute(
        "UPDATE jobs SET state = 'dead', last_error = 'step died'"
        " WHERE id = %(id)s",
        {"id": step_job["id"]},
    )

    letters = dlq.list_dead(conn, limit=10)
    assert len(letters) == 2
    plain = next(letter for letter in letters if letter.id == plain_id)
    assert plain.type == "plain"
    assert plain.workflow_id is None and plain.step_name is None
    assert plain.attempts == 2 and plain.replay_count == 0

    stepped = next(letter for letter in letters if letter.id == step_job["id"])
    assert stepped.workflow_id == wf.id
    assert stepped.workflow_type == "chain"
    assert stepped.step_name == "a" and stepped.step_position == 0
    assert stepped.last_error == "step died"


def test_replay_extends_budget_without_touching_attempts(
    clean_db: Settings,
) -> None:
    conn = db.connect(_fast(clean_db))
    job_id = _make_dead_plain_job(conn, "replay-1")

    result = dlq.replay(conn, job_id, budget=3)
    assert result.state == "queued"
    assert result.attempts == 2, "attempts is claim-owned and never rewound"
    assert result.max_attempts == 5, "attempts + budget, not a reset"
    assert result.replay_count == 1
    assert result.workflow_restarted is False

    row = conn.execute(
        "SELECT state, attempts, max_attempts, replay_count, run_at"
        " FROM jobs WHERE id = %(id)s",
        {"id": job_id},
    ).fetchone()
    assert row is not None
    assert row["state"] == "queued"
    assert row["max_attempts"] == 5
    gap = (row["run_at"] - conn.execute("SELECT now() AS t").fetchone()["t"]).total_seconds()
    assert -1.0 <= gap <= 1.0, "run_at = now()"

    again = dlq.replay(
        conn,
        conn.execute(
            "UPDATE jobs SET state = 'dead' WHERE id = %(id)s RETURNING id",
            {"id": job_id},
        ).fetchone()["id"],
        budget=3,
    )
    assert again.replay_count == 2
    assert again.max_attempts == 5, "the budget extends from attempts again"


def test_replay_refuses_jobs_that_are_not_dead(clean_db: Settings) -> None:
    conn = db.connect(_fast(clean_db))
    for state, extra in (
        ("succeeded", ", result = '{}'::jsonb"),
        ("running", ", locked_by = 'w', lease_expires_at = now() + interval '1 hour'"),
        ("queued", ""),
    ):
        job = jobs.enqueue(conn, job_type="plain", dedupe_key="replay-2")
        conn.execute(
            f"UPDATE jobs SET state = %(s)s{extra} WHERE id = %(id)s",
            {"s": state, "id": job.id},
        )
        with pytest.raises(dlq.NotReplayable, match="not 'dead'"):
            dlq.replay(conn, job.id, budget=3)

    with pytest.raises(dlq.NotReplayable, match="not found"):
        dlq.replay(conn, uuid4(), budget=3)


def test_replay_resumes_a_failed_workflow(clean_db: Settings) -> None:
    settings = _fast(clean_db)
    conn = db.connect(settings)
    calls: dict[str, int] = {}
    definition = _two_step(calls)
    wf = workflows.start_workflow(
        conn, wf_type="chain", definition=definition
    )
    step_job = conn.execute(
        "SELECT id FROM jobs WHERE dedupe_key = %(k)s",
        {"k": workflows.step_job_dedupe_key(wf.id, 0)},
    ).fetchone()
    assert step_job is not None
    conn.execute(
        "UPDATE jobs SET state = 'dead', last_error = 'step died'"
        " WHERE id = %(id)s",
        {"id": step_job["id"]},
    )
    counts = reconciler.reconcile(conn, settings=settings)
    assert counts["dead_steps"] == 1

    result = dlq.replay(conn, step_job["id"], budget=2)
    assert result.workflow_restarted is True

    wf_row = conn.execute(
        "SELECT state, failed_step FROM workflows WHERE id = %(id)s",
        {"id": wf.id},
    ).fetchone()
    assert wf_row is not None
    assert wf_row["state"] == "running"
    assert wf_row["failed_step"] is None
    step0 = conn.execute(
        "SELECT state FROM workflow_steps"
        " WHERE workflow_id = %(id)s AND position = 0",
        {"id": wf.id},
    ).fetchone()
    assert step0 is not None and step0["state"] == "pending"

    # the replayed budget is live: the worker finishes the workflow
    worker, thread = _run_worker(
        settings,
        workflows.make_workflow_handlers({"chain": definition}, settings=settings),
    )
    try:
        _wait_until(lambda: _state(conn, wf.id) == "succeeded")
        assert calls == {"a": 1, "b": 1}
    finally:
        _stop(worker, thread)


def test_replay_retries_once_on_deadlock(
    clean_db: Settings, monkeypatch: Any
) -> None:
    conn = db.connect(_fast(clean_db))
    job_id = _make_dead_plain_job(conn, "replay-deadlock")
    real = dlq._replay_once
    calls = {"n": 0}

    def flaky(conn_: Any, job_id_: Any, budget: int) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise psycopg.errors.DeadlockDetected("deadlock detected")
        return real(conn_, job_id_, budget)

    monkeypatch.setattr(dlq, "_replay_once", flaky)
    result = dlq.replay(conn, job_id, budget=3)
    assert calls["n"] == 2
    assert result.state == "queued"

    def always(conn_: Any, job_id_: Any, budget: int) -> Any:
        calls["n"] += 1
        raise psycopg.errors.DeadlockDetected("deadlock detected")

    monkeypatch.setattr(dlq, "_replay_once", always)
    before = calls["n"]
    with pytest.raises(psycopg.errors.DeadlockDetected):
        dlq.replay(conn, job_id, budget=3)
    assert calls["n"] == before + 2, "exactly one retry, then it propagates"


def test_dlq_cli_lists_and_replays(clean_db: Settings, monkeypatch: Any, capsys: Any) -> None:
    settings = _fast(clean_db)
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    conn = db.connect(settings)
    job_id = _make_dead_plain_job(conn, "cli-1")
    conn.close()

    assert dlq.main(["list"]) == 0
    out = capsys.readouterr().out
    assert str(job_id) in out
    assert "boom" in out

    assert dlq.main(["replay", str(job_id), "--budget", "4"]) == 0
    out = capsys.readouterr().out
    assert "replayed" in out
    assert "plain job" in out

    assert dlq.main(["replay", str(job_id)]) == 1
    err = capsys.readouterr().err
    assert "refused" in err, "a queued job is not replayable"


def _state(conn: Any, wf_id: Any) -> Any:
    row = conn.execute(
        "SELECT state FROM workflows WHERE id = %(id)s", {"id": wf_id}
    ).fetchone()
    assert row is not None
    return row["state"]
