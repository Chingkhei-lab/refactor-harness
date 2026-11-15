"""Checkpoint 4: durable workflows — atomic start, dedupe, ordering, resume."""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from typing import Any

import pytest

from engine import db, jobs, workflows
from engine.config import Settings
from engine.worker import Worker
from engine.workflows import StepContext, StepDef, Workflow, WorkflowDef


def _bump(calls: dict[str, int], name: str) -> int:
    calls[name] = calls.get(name, 0) + 1
    return calls[name]


def _chain(
    calls: dict[str, int], order: list[str], b_failures: int = 0
) -> WorkflowDef:
    """a -> b -> c with chained outputs; b can be made to fail N times."""

    def a(ctx: StepContext, prior: dict[str, Any]) -> dict[str, Any]:
        _bump(calls, "a")
        order.append("a")
        return {"a": 1}

    def b(ctx: StepContext, prior: dict[str, Any]) -> dict[str, Any]:
        run = _bump(calls, "b")
        order.append("b")
        if run <= b_failures:
            raise RuntimeError("transient crash in b")
        return {"b": prior["a"]["a"] + 1}

    def c(ctx: StepContext, prior: dict[str, Any]) -> dict[str, Any]:
        _bump(calls, "c")
        order.append("c")
        return {"c": prior["b"]["b"] * 2}

    return WorkflowDef(
        type="chain", steps=(StepDef("a", a), StepDef("b", b), StepDef("c", c))
    )


def _worker_settings(settings: Settings) -> Settings:
    return replace(
        settings,
        backoff_base_seconds=0.05,
        backoff_cap_seconds=0.5,
        idle_sleep_min_seconds=0.02,
        idle_sleep_max_seconds=0.05,
    )


def _run_worker(
    settings: Settings, handlers: dict[str, Any]
) -> tuple[Worker, threading.Thread]:
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


def _count(conn: Any, table: str) -> int:
    row = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
    assert row is not None
    return int(row["n"])


def _row(conn: Any, sql: str, params: dict[str, Any] | None = None) -> Any:
    row = conn.execute(sql, params or {}).fetchone()
    assert row is not None
    return row


def _workflow_state(conn: Any, workflow_id: Any) -> Any:
    return _row(
        conn, "SELECT state FROM workflows WHERE id = %(id)s", {"id": workflow_id}
    )["state"]


def _step_outputs(conn: Any, workflow_id: Any) -> list[Any]:
    rows = conn.execute(
        "SELECT position, name, state, output FROM workflow_steps"
        " WHERE workflow_id = %(id)s ORDER BY position",
        {"id": workflow_id},
    ).fetchall()
    return [row["output"] for row in rows]


def test_start_workflow_is_atomic(clean_db: Settings, monkeypatch: Any) -> None:
    conn = db.connect(clean_db)
    calls: dict[str, int] = {}
    order: list[str] = []
    definition = _chain(calls, order)

    def boom(*args: Any, **kwargs: Any) -> jobs.Job:
        raise RuntimeError("enqueue blew up mid-start")

    # simulate a crash after the workflow and step rows were written: the
    # first-step enqueue fails inside the start transaction
    monkeypatch.setattr(workflows.jobs, "enqueue", boom)
    with pytest.raises(RuntimeError, match="enqueue blew up"):
        workflows.start_workflow(
            conn, wf_type="chain", definition=definition, dedupe_key="wf-1"
        )
    monkeypatch.undo()

    # rollback: nothing persisted at any stage
    assert _count(conn, "workflows") == 0
    assert _count(conn, "workflow_steps") == 0
    assert _count(conn, "jobs") == 0

    # the connection is still usable; a real start persists exactly 1/3/1
    workflows.start_workflow(
        conn, wf_type="chain", definition=definition, dedupe_key="wf-1"
    )
    assert _count(conn, "workflows") == 1
    assert _count(conn, "workflow_steps") == 3
    assert _count(conn, "jobs") == 1


def test_start_workflow_dedupe_returns_existing(clean_db: Settings) -> None:
    conn = db.connect(clean_db)
    calls: dict[str, int] = {}
    order: list[str] = []
    definition = _chain(calls, order)

    wf1 = workflows.start_workflow(
        conn, wf_type="chain", definition=definition, input={"x": 1}, dedupe_key="wf-1"
    )
    wf2 = workflows.start_workflow(
        conn, wf_type="chain", definition=definition, input={"x": 999}, dedupe_key="wf-1"
    )

    assert wf1.id == wf2.id
    assert wf2.state == "running"
    assert wf2.input == {"x": 1}, "the first input wins"
    assert _count(conn, "workflows") == 1
    assert _count(conn, "workflow_steps") == 3
    assert _count(conn, "jobs") == 1
    job0 = _row(
        conn,
        "SELECT state, max_attempts FROM jobs WHERE dedupe_key = %(k)s",
        {"k": workflows.step_job_dedupe_key(wf1.id, 0)},
    )
    assert job0["state"] == "queued"
    assert job0["max_attempts"] == 5  # from the step definition


def test_start_workflow_dedupe_under_a_race(clean_db: Settings) -> None:
    calls: dict[str, int] = {}
    order: list[str] = []
    definition = _chain(calls, order)
    barrier = threading.Barrier(2)
    results: list[Workflow] = []

    def start() -> None:
        conn = db.connect(clean_db)
        barrier.wait()
        results.append(
            workflows.start_workflow(
                conn, wf_type="chain", definition=definition, dedupe_key="race"
            )
        )

    threads = [threading.Thread(target=start) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    conn = db.connect(clean_db)
    assert len({str(w.id) for w in results}) == 1
    assert all(w.state == "running" for w in results)
    assert _count(conn, "workflows") == 1
    assert _count(conn, "workflow_steps") == 3
    assert _count(conn, "jobs") == 1


def test_steps_run_in_order_and_outputs_flow(clean_db: Settings) -> None:
    settings = _worker_settings(clean_db)
    conn = db.connect(settings)
    calls: dict[str, int] = {}
    order: list[str] = []
    definition = _chain(calls, order)
    wf = workflows.start_workflow(conn, wf_type="chain", definition=definition)

    worker, thread = _run_worker(
        settings, workflows.make_workflow_handlers({"chain": definition}, settings=settings)
    )
    try:
        _wait_until(lambda: _workflow_state(conn, wf.id) == "succeeded")
        assert order == ["a", "b", "c"], "steps must run strictly in order"
        assert calls == {"a": 1, "b": 1, "c": 1}
        assert _step_outputs(conn, wf.id) == [{"a": 1}, {"b": 2}, {"c": 4}]
        results = conn.execute(
            "SELECT result FROM jobs WHERE type = 'workflow_step'"
            " ORDER BY created_at, id"
        ).fetchall()
        assert [row["result"]["position"] for row in results] == [0, 1, 2]
    finally:
        _stop(worker, thread)


def test_crash_mid_step_resumes_at_that_step(clean_db: Settings) -> None:
    settings = _worker_settings(clean_db)
    conn = db.connect(settings)
    calls: dict[str, int] = {}
    order: list[str] = []
    definition = _chain(calls, order, b_failures=1)
    wf = workflows.start_workflow(conn, wf_type="chain", definition=definition)

    worker, thread = _run_worker(
        settings, workflows.make_workflow_handlers({"chain": definition}, settings=settings)
    )
    try:
        _wait_until(lambda: _workflow_state(conn, wf.id) == "succeeded")
        # the crash happened inside b: it ran twice, a was never re-run, and
        # c only ran after b's second attempt committed
        assert calls == {"a": 1, "b": 2, "c": 1}
        assert order == ["a", "b", "b", "c"]
        job_b = _row(
            conn,
            "SELECT attempts FROM jobs WHERE dedupe_key = %(k)s",
            {"k": workflows.step_job_dedupe_key(wf.id, 1)},
        )
        assert job_b["attempts"] == 2
        assert _step_outputs(conn, wf.id) == [{"a": 1}, {"b": 2}, {"c": 4}]
    finally:
        _stop(worker, thread)


def test_committed_step_is_not_rerun(clean_db: Settings) -> None:
    settings = _worker_settings(clean_db)
    conn = db.connect(settings)
    calls: dict[str, int] = {}
    order: list[str] = []
    definition = _chain(calls, order)
    wf = workflows.start_workflow(conn, wf_type="chain", definition=definition)

    worker, thread = _run_worker(
        settings, workflows.make_workflow_handlers({"chain": definition}, settings=settings)
    )
    try:
        _wait_until(lambda: _workflow_state(conn, wf.id) == "succeeded")
        assert calls == {"a": 1, "b": 1, "c": 1}

        # force a redelivery of b's committed job, as a manual repair would
        key = workflows.step_job_dedupe_key(wf.id, 1)
        conn.execute(
            "UPDATE jobs SET state = 'queued', run_at = now(), locked_by = NULL,"
            " lease_expires_at = NULL, last_heartbeat_at = NULL"
            " WHERE dedupe_key = %(k)s",
            {"k": key},
        )
        _wait_until(
            lambda: _row(
                conn, "SELECT state FROM jobs WHERE dedupe_key = %(k)s", {"k": key}
            )["state"]
            == "succeeded"
        )
        assert calls["b"] == 1, "a committed step must not re-run"
        result = _row(
            conn, "SELECT result FROM jobs WHERE dedupe_key = %(k)s", {"k": key}
        )["result"]
        assert result.get("resumed") is True
        step_b = _row(
            conn,
            "SELECT state, output FROM workflow_steps"
            " WHERE workflow_id = %(id)s AND position = 1",
            {"id": wf.id},
        )
        assert step_b["state"] == "succeeded"
        assert step_b["output"] == {"b": 2}
        assert _workflow_state(conn, wf.id) == "succeeded"
    finally:
        _stop(worker, thread)


def test_stale_worker_output_is_discarded(clean_db: Settings) -> None:
    conn_admin = db.connect(clean_db)
    calls: dict[str, int] = {}
    order: list[str] = []
    definition = _chain(calls, order)
    wf = workflows.start_workflow(conn_admin, wf_type="chain", definition=definition)

    conn_a = db.connect(clean_db)
    job_a = jobs.claim(
        conn_a, locked_by="worker-a", lease_seconds=30.0,
        types=[workflows.WORKFLOW_JOB_TYPE],
    )
    assert job_a is not None and job_a.attempts == 1

    # the reaper reclaims worker-a's lease; worker-b claims the next attempt
    conn_admin.execute(
        "UPDATE jobs SET state = 'queued', run_at = now(), locked_by = NULL,"
        " lease_expires_at = NULL, last_heartbeat_at = NULL WHERE state = 'running'"
    )
    conn_b = db.connect(clean_db)
    job_b = jobs.claim(
        conn_b, locked_by="worker-b", lease_seconds=30.0,
        types=[workflows.WORKFLOW_JOB_TYPE],
    )
    assert job_b is not None and job_b.attempts == 2
    workflow_row, steps = workflows.load_workflow_state(conn_b, wf.id)

    # the stale attempt's advance transaction: the fence rejects its complete,
    # so the whole transaction — including its output — is rolled back
    with pytest.raises(jobs.LeaseLost):
        with conn_a.transaction():
            jobs.complete(
                conn_a, job_id=job_a.id, locked_by="worker-a", attempts=1,
                result={"stale": True},
            )
            workflows._advance_after_complete(
                conn_a, workflow=workflow_row, definition=definition,
                steps=steps, position=0, output={"stale": True},
            )

    step0 = _row(
        conn_admin,
        "SELECT state, output FROM workflow_steps"
        " WHERE workflow_id = %(id)s AND position = 0",
        {"id": wf.id},
    )
    assert step0["state"] == "pending"
    assert step0["output"] is None
    assert _count(conn_admin, "jobs") == 1, "no next job may leak from a stale attempt"

    # the live attempt's advance persists its own output and opens step 1
    with conn_b.transaction():
        completed = jobs.complete(
            conn_b, job_id=job_b.id, locked_by="worker-b", attempts=2,
            result={"ok": True},
        )
        assert completed.state == "succeeded"
        workflows._advance_after_complete(
            conn_b, workflow=workflow_row, definition=definition,
            steps=steps, position=0, output={"a": 1},
        )

    step0 = _row(
        conn_admin,
        "SELECT state, output FROM workflow_steps"
        " WHERE workflow_id = %(id)s AND position = 0",
        {"id": wf.id},
    )
    assert step0["state"] == "succeeded"
    assert step0["output"] == {"a": 1}
    next_job = _row(
        conn_admin,
        "SELECT state, attempts FROM jobs WHERE dedupe_key = %(k)s",
        {"k": workflows.step_job_dedupe_key(wf.id, 1)},
    )
    assert next_job["state"] == "queued"
    assert next_job["attempts"] == 0
    assert _workflow_state(conn_admin, wf.id) == "running"
