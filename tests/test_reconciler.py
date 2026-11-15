"""Checkpoint 5: the reconciler's three rules and the replay race."""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from typing import Any

import psycopg

from engine import db, jobs, reconciler, workflows
from engine import dlq as dlq_module
from engine.config import Settings
from engine.worker import Worker
from engine.workflows import StepContext, StepDef, Workflow, WorkflowDef


def _fast(settings: Settings) -> Settings:
    return replace(
        settings,
        backoff_base_seconds=0.05,
        backoff_cap_seconds=0.5,
        idle_sleep_min_seconds=0.02,
        idle_sleep_max_seconds=0.05,
        reconciler_interval_seconds=0.2,
    )


def _count_jobs(conn: Any) -> int:
    row = conn.execute("SELECT count(*) AS n FROM jobs").fetchone()
    assert row is not None
    return int(row["n"])


def _row_now(conn: Any) -> Any:
    row = conn.execute("SELECT now() AS t").fetchone()
    assert row is not None
    return row["t"]


def _two_step(
    calls: dict[str, int], a_failures: int = 0
) -> WorkflowDef:
    def a(ctx: StepContext, prior: dict[str, Any]) -> dict[str, Any]:
        calls["a"] = calls.get("a", 0) + 1
        if calls["a"] <= a_failures:
            raise RuntimeError("transient")
        return {"a": 1}

    def b(ctx: StepContext, prior: dict[str, Any]) -> dict[str, Any]:
        calls["b"] = calls.get("b", 0) + 1
        return {"b": prior["a"]["a"] + 1}

    return WorkflowDef(type="chain", steps=(StepDef("a", a), StepDef("b", b)))


def _kill_step_job(
    conn: Any, wf: Workflow, position: int, error: str = "boom"
) -> None:
    conn.execute(
        "UPDATE jobs SET state = 'dead', last_error = %(e)s"
        " WHERE dedupe_key = %(k)s",
        {"e": error, "k": workflows.step_job_dedupe_key(wf.id, position)},
    )


def _wf_row(conn: Any, wf: Workflow) -> Any:
    row = conn.execute(
        "SELECT state, failed_step, last_error FROM workflows WHERE id = %(id)s",
        {"id": wf.id},
    ).fetchone()
    assert row is not None
    return row


def _step_row(conn: Any, wf: Workflow, position: int) -> Any:
    row = conn.execute(
        "SELECT state, output FROM workflow_steps"
        " WHERE workflow_id = %(id)s AND position = %(p)s",
        {"id": wf.id, "p": position},
    ).fetchone()
    assert row is not None
    return row


def _step_job(conn: Any, wf: Workflow, position: int) -> Any:
    row = conn.execute(
        "SELECT state, attempts, max_attempts, run_at, last_error FROM jobs"
        " WHERE dedupe_key = %(k)s",
        {"k": workflows.step_job_dedupe_key(wf.id, position)},
    ).fetchone()
    assert row is not None
    return row


def test_rule1_dead_current_step_fails_step_and_workflow(
    clean_db: Settings,
) -> None:
    settings = _fast(clean_db)
    conn = db.connect(settings)
    calls: dict[str, int] = {}
    wf = workflows.start_workflow(
        conn, wf_type="chain", definition=_two_step(calls)
    )
    _kill_step_job(conn, wf, 0, error="permanent failure")

    counts = reconciler.reconcile(conn, settings=settings)
    assert counts["dead_steps"] == 1

    step0 = _step_row(conn, wf, 0)
    assert step0["state"] == "failed"
    wf_row = _wf_row(conn, wf)
    assert wf_row["state"] == "failed"
    assert wf_row["failed_step"] == "a"
    assert wf_row["last_error"] == "permanent failure"
    job = _step_job(conn, wf, 0)
    assert job["state"] == "dead", "the reconciler never touches the job"

    # the second cycle is a no-op: already-failed states stay terminal
    counts2 = reconciler.reconcile(conn, settings=settings)
    assert counts2 == {"dead_steps": 0, "missing_jobs": 0, "inconsistent": 0}


def test_rule1_ignores_a_later_dead_job(clean_db: Settings) -> None:
    """A dead job for a non-current step (manual mess) must not advance."""
    settings = _fast(clean_db)
    conn = db.connect(settings)
    calls: dict[str, int] = {}
    wf = workflows.start_workflow(
        conn, wf_type="chain", definition=_two_step(calls)
    )
    # pretend step 1 also has a dead job row (unreachable through the engine)
    conn.execute(
        "INSERT INTO jobs (type, payload, state, attempts, max_attempts,"
        " run_at, dedupe_key, last_error)"
        " VALUES ('workflow_step', %(payload)s, 'dead', 3, 3, now(),"
        " %(key)s, 'ghost')",
        {
            "payload": f'{{"workflow_id": "{wf.id}", "position": 1}}',
            "key": workflows.step_job_dedupe_key(wf.id, 1),
        },
    )
    counts = reconciler.reconcile(conn, settings=settings)
    assert counts["dead_steps"] == 0, "only the CURRENT step's dead job counts"
    assert _wf_row(conn, wf)["state"] == "running"


def test_rule2_missing_job_is_reenqueued_with_the_snapshot(
    clean_db: Settings,
) -> None:
    settings = _fast(clean_db)
    conn = db.connect(settings)
    calls: dict[str, int] = {}

    def a(ctx: StepContext, prior: dict[str, Any]) -> dict[str, Any]:
        calls["a"] = calls.get("a", 0) + 1
        return {"a": 1}

    definition = WorkflowDef(
        type="chain",
        steps=(StepDef("a", a, delay_seconds=5.0, max_attempts=3),),
    )
    wf = workflows.start_workflow(
        conn, wf_type="chain", definition=definition
    )
    key = workflows.step_job_dedupe_key(wf.id, 0)
    conn.execute("DELETE FROM jobs WHERE dedupe_key = %(k)s", {"k": key})

    counts = reconciler.reconcile(conn, settings=settings)
    assert counts["missing_jobs"] == 1
    assert _count_jobs(conn) == 1, "exactly one job for the step's key"

    job = _step_job(conn, wf, 0)
    assert job["state"] == "queued"
    assert job["attempts"] == 0
    assert job["max_attempts"] == 3, "the snapshot's retry budget"
    gap = (job["run_at"] - _row_now(conn)).total_seconds()
    assert gap >= 4.0, f"the snapshot's delay re-applied (gap={gap}s)"
    assert _wf_row(conn, wf)["state"] == "running"

    counts2 = reconciler.reconcile(conn, settings=settings)
    assert counts2["missing_jobs"] == 0, "idempotent once the job exists"


def test_rule3_succeeded_job_with_pending_step_is_surfaced_not_looped(
    clean_db: Settings,
) -> None:
    settings = _fast(clean_db)
    conn = db.connect(settings)
    calls: dict[str, int] = {}
    wf = workflows.start_workflow(
        conn, wf_type="chain", definition=_two_step(calls)
    )
    key = workflows.step_job_dedupe_key(wf.id, 0)
    conn.execute(
        "UPDATE jobs SET state = 'succeeded', result = '{}'::jsonb"
        " WHERE dedupe_key = %(k)s",
        {"k": key},
    )

    counts = reconciler.reconcile(conn, settings=settings)
    assert counts["inconsistent"] == 1
    wf_row = _wf_row(conn, wf)
    assert wf_row["state"] == "failed"
    assert wf_row["failed_step"] == "a"
    assert "inconsistent state" in wf_row["last_error"]

    # never re-enqueue: the job stays succeeded and repeated cycles do not loop
    for _ in range(3):
        counts = reconciler.reconcile(conn, settings=settings)
        assert counts["inconsistent"] == 0
    job = _step_job(conn, wf, 0)
    assert job["state"] == "succeeded"
    assert _count_jobs(conn) == 1


def test_reconciler_noops_on_a_healthy_workflow(clean_db: Settings) -> None:
    settings = _fast(clean_db)
    conn = db.connect(settings)
    calls: dict[str, int] = {}
    wf = workflows.start_workflow(
        conn, wf_type="chain", definition=_two_step(calls)
    )
    counts = reconciler.reconcile(conn, settings=settings)
    assert counts == {"dead_steps": 0, "missing_jobs": 0, "inconsistent": 0}
    assert _wf_row(conn, wf)["state"] == "running"
    assert _step_job(conn, wf, 0)["state"] == "queued"


def test_nonretryable_step_dies_and_reconciler_fails_the_workflow(
    clean_db: Settings,
) -> None:
    """End to end: NonRetryableError -> force_dead -> reconciler -> failed."""
    settings = _fast(clean_db)
    conn = db.connect(settings)
    calls: dict[str, int] = {}

    def a(ctx: StepContext, prior: dict[str, Any]) -> dict[str, Any]:
        raise jobs.NonRetryableError("bad workflow input")

    def b(ctx: StepContext, prior: dict[str, Any]) -> dict[str, Any]:
        calls["b"] = calls.get("b", 0) + 1
        return {"b": 2}

    definition = WorkflowDef(type="chain", steps=(StepDef("a", a), StepDef("b", b)))
    wf = workflows.start_workflow(
        conn, wf_type="chain", definition=definition
    )
    worker = Worker(
        settings,
        workflows.make_workflow_handlers({"chain": definition}, settings=settings),
        application_name="engine-worker-test",
    )
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if _wf_row(conn, wf)["state"] == "failed":
                break
            time.sleep(0.05)
        else:
            raise AssertionError("workflow did not fail in time")
    finally:
        worker.request_stop()
        thread.join(timeout=10)
        assert not thread.is_alive()

    wf_row = _wf_row(conn, wf)
    assert wf_row["failed_step"] == "a"
    assert "non-retryable: bad workflow input" in wf_row["last_error"]
    step0 = _step_row(conn, wf, 0)
    assert step0["state"] == "failed"
    job = _step_job(conn, wf, 0)
    assert job["state"] == "dead"
    assert job["attempts"] == 1, "non-retryable: no second attempt"
    assert calls.get("b") is None, "the workflow never reached step b"


def test_worker_reconcile_hook_tolerates_deadlock(
    clean_db: Settings, monkeypatch: Any
) -> None:
    settings = _fast(clean_db)
    worker = Worker(settings, {}, application_name="engine-worker-test")
    calls = {"n": 0}

    def flaky(conn: Any, settings: Any) -> dict[str, int]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise psycopg.errors.DeadlockDetected("deadlock detected")
        return {"dead_steps": 0, "missing_jobs": 0, "inconsistent": 0}

    monkeypatch.setattr(reconciler, "reconcile", flaky)

    worker._last_reconcile = 0.0
    worker._reconcile_if_due()  # DeadlockDetected is swallowed by the rule
    assert calls["n"] == 1

    worker._last_reconcile = 0.0
    worker._reconcile_if_due()  # the next cycle retries and returns counts
    assert calls["n"] == 2


def test_replay_races_reconciler_final_state_is_legal(
    clean_db: Settings,
) -> None:
    """Replay vs reconciler on the same workflow, many rounds.

    Both sides lock in the global order job -> step -> workflow, so no
    deadlock can leave a partial write: the final state is always one of
    the two legal whole-outcomes:

    - (workflow running, step pending, job queued)   — replay applied
    - (workflow failed, step failed, job dead)       — reconciler applied

    Anything else (e.g. queued job on a failed workflow) would mean one
    side's transaction was torn, which the lock order makes impossible.
    """
    settings = _fast(clean_db)
    conn = db.connect(settings)
    legal = {
        ("running", "pending", "queued"),
        ("failed", "failed", "dead"),
    }
    for round_no in range(30):
        calls: dict[str, int] = {}
        definition = _two_step(calls)
        wf = workflows.start_workflow(
            conn,
            wf_type="chain",
            definition=definition,
            dedupe_key=f"race-{round_no}",
        )
        job_id = conn.execute(
            "SELECT id FROM jobs WHERE dedupe_key = %(k)s",
            {"k": workflows.step_job_dedupe_key(wf.id, 0)},
        ).fetchone()
        assert job_id is not None
        _kill_step_job(conn, wf, 0, error="boom")

        barrier = threading.Barrier(2)

        def run_replay(
            barrier: threading.Barrier = barrier, job_id: Any = job_id
        ) -> None:
            rc = db.connect(settings)
            barrier.wait()
            try:
                dlq_module.replay(rc, job_id["id"], budget=3)
            except psycopg.errors.DeadlockDetected:
                pass  # a legal give-up after the single retry

        def run_reconcile(barrier: threading.Barrier = barrier) -> None:
            rc = db.connect(settings)
            barrier.wait()
            reconciler.reconcile(rc, settings=settings)

        t1 = threading.Thread(target=run_replay)
        t2 = threading.Thread(target=run_reconcile)
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)
        assert not t1.is_alive() and not t2.is_alive()

        final = conn.execute(
            "SELECT wf.state AS wf_state, s.state AS step_state,"
            " j.state AS job_state"
            " FROM workflows wf"
            " JOIN workflow_steps s ON s.workflow_id = wf.id"
            " AND s.position = 0"
            " JOIN jobs j ON j.dedupe_key = %(k)s"
            " WHERE wf.id = %(id)s",
            {"k": workflows.step_job_dedupe_key(wf.id, 0), "id": wf.id},
        ).fetchone()
        assert final is not None
        outcome = (final["wf_state"], final["step_state"], final["job_state"])
        assert outcome in legal, f"round {round_no}: illegal state {outcome}"
