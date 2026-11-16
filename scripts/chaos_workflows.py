"""Chaos v2 exit check: durable workflows under hard kills (checkpoint 6).

Run on Linux (Docker) — Windows cannot signal processes reliably:

    docker run --rm --add-host=host.docker.internal:host-gateway \
      -v "$PWD":/app -w /app \
      -e DATABASE_URL=postgresql://app:app@host.docker.internal:5433/app_test \
      python:3.12-slim \
      bash -c "pip install -q 'psycopg[binary]' alembic && python scripts/chaos_workflows.py"

What it does: creates effects/executions/violations/pardon (test scaffolding,
outside Alembic), starts 2,000 workflows of 4 steps each (one 2-second delay
step; 2% always-failing + 1% NonRetryableError injected deterministically via
random.Random(42)), runs 3 worker processes, hard-kills a random worker every
3-6 s (logging whether the victim held a running job) and starts a
replacement, then waits for the drain: every workflow terminal and no
queued/running jobs left (10-minute timeout = failure).

Gates (all required): every workflow terminal; every succeeded workflow has
exactly one effect row per step with outputs matching the running-sum chain
[1, 3, 6, 10]; failed workflows == injected count and each in dead_letters;
no claim violations; and — after the run — one failed workflow is pardoned,
replayed via engine.dlq, and finishes with the full effect chain.

The report lands in docs/results/chaos-v2-<date>.json. Reconciler latency
(job death -> workflow failed) is measured BEFORE the replay phase (replay
bumps the stamps it is derived from).
"""

from __future__ import annotations

import json
import os
import platform
import random
import subprocess
import sys
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from chaos_workflow_handlers import DELAY_STEP_SECONDS, SUM_BUDGET, WORKFLOWS  # noqa: E402

from engine import db, dlq, workflows  # noqa: E402
from engine.config import load_settings  # noqa: E402

TOTAL_WORKFLOWS = 2_000
STEPS_PER_WORKFLOW = 4
EXPECTED_SUMS = [1, 3, 6, 10]
INJECTION_SEED = 42
WORKER_COUNT = 3
KILL_INTERVAL_RANGE = (3.0, 6.0)
DRAIN_TIMEOUT_SECONDS = 600.0
LEASE_SECONDS = 4.0
RECONCILER_INTERVAL_SECONDS = 1.0
BACKOFF_BASE_SECONDS = 0.2
BACKOFF_CAP_SECONDS = 5.0
REPLAY_BUDGET = 3
REPLAY_TIMEOUT_SECONDS = 120.0
RESULTS_DIR = _REPO_ROOT / "docs" / "results"
HANDLER_MODULE = str(_REPO_ROOT / "scripts" / "chaos_workflow_handlers.py")

_SCAFFOLD_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS effects (
        effect_key TEXT PRIMARY KEY,
        workflow_id UUID NOT NULL,
        step_name TEXT NOT NULL,
        output JSONB NOT NULL,
        job_id UUID NOT NULL,
        attempt INTEGER NOT NULL,
        worker TEXT NOT NULL,
        at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS executions (
        job_id UUID NOT NULL,
        attempt INTEGER NOT NULL,
        effect_key TEXT NOT NULL,
        worker TEXT NOT NULL,
        at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (job_id, attempt)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS violations (
        job_id UUID NOT NULL,
        attempt INTEGER NOT NULL,
        worker TEXT NOT NULL,
        at TIMESTAMPTZ NOT NULL DEFAULT now(),
        detail TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pardon (
        job_id UUID PRIMARY KEY,
        at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
]

_SUMS_CHAIN_SQL = """
SELECT count(*) AS n
FROM workflow_steps s
JOIN workflows wf ON wf.id = s.workflow_id AND wf.state = 'succeeded'
WHERE COALESCE((s.output ->> 'sum')::int, -1) <> CASE s.position
    WHEN 0 THEN %(s0)s WHEN 1 THEN %(s1)s WHEN 2 THEN %(s2)s WHEN 3 THEN %(s3)s
END
"""

_DEAD_LETTER_GAPS_SQL = """
SELECT count(*) AS n
FROM workflows wf
WHERE wf.state = 'failed'
  AND NOT EXISTS (SELECT 1 FROM dead_letters d WHERE d.workflow_id = wf.id)
"""


def _inject_counts(total: int) -> tuple[int, int]:
    """2% always-failing + 1% NonRetryable, min one each (2000 -> 40 + 20)."""
    return max(1, round(total * 0.02)), max(1, round(total * 0.01))


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise SystemExit("DATABASE_URL is required (chaos refuses defaults)")
    dbname = urlsplit(url).path.lstrip("/")
    if not dbname.endswith("_test") and os.environ.get("CHAOS_ALLOW_ANY_DB") != "1":
        raise SystemExit(
            f"refusing to run chaos against database {dbname!r}: the name must "
            "end with '_test' (or set CHAOS_ALLOW_ANY_DB=1)"
        )
    return url


def _machine_specs() -> dict[str, Any]:
    ram_gb = None
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    ram_gb = round(int(line.split()[1]) / 1024 / 1024, 1)
                    break
    except OSError:
        ram_gb = None
    return {
        "runner": "Docker container (Linux): Windows host cannot signal processes reliably",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpus": os.cpu_count(),
        "ram_gb": ram_gb,
    }


def _count(conn: Any, sql: str, params: dict[str, Any] | None = None) -> int:
    row = conn.execute(sql, params or {}).fetchone()
    return int(row["n"]) if row is not None else 0


def _sums_chain_bad(conn: Any, workflow_id: Any = None) -> int:
    params: dict[str, Any] = dict(
        zip(("s0", "s1", "s2", "s3"), EXPECTED_SUMS, strict=True)
    )
    sql = _SUMS_CHAIN_SQL
    if workflow_id is not None:
        sql += " AND s.workflow_id = %(workflow_id)s"
        params["workflow_id"] = workflow_id
    return _count(conn, sql, params)


def _stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    n = len(ordered)

    def pct(p: float) -> float:
        index = min(n - 1, max(0, int(round(p * (n - 1)))))
        return round(ordered[index], 3)

    return {
        "count": n,
        "min_seconds": round(ordered[0], 3),
        "p50_seconds": pct(0.50),
        "p95_seconds": pct(0.95),
        "max_seconds": round(ordered[-1], 3),
        "avg_seconds": round(sum(ordered) / n, 3),
    }


def main() -> int:
    url = _database_url()
    total = int(os.environ.get("CHAOS_TOTAL_WORKFLOWS", TOTAL_WORKFLOWS))
    drain_timeout = float(
        os.environ.get("CHAOS_DRAIN_TIMEOUT_SECONDS", DRAIN_TIMEOUT_SECONDS)
    )
    inject_fail, inject_nr = _inject_counts(total)
    os.environ["ALEMBIC_DATABASE_URL"] = url
    alembic_config = Config(str(_REPO_ROOT / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    command.upgrade(alembic_config, "head")

    settings = replace(
        load_settings(),
        database_url=url,
        lease_seconds=LEASE_SECONDS,
        heartbeat_interval_seconds=LEASE_SECONDS / 3,
        reaper_interval_seconds=1.0,
        reconciler_interval_seconds=RECONCILER_INTERVAL_SECONDS,
        backoff_base_seconds=BACKOFF_BASE_SECONDS,
        backoff_cap_seconds=BACKOFF_CAP_SECONDS,
        idle_sleep_min_seconds=0.05,
        idle_sleep_max_seconds=0.2,
    )
    conn = db.connect(settings, application_name="engine-chaos-v2")

    for statement in _SCAFFOLD_TABLES_SQL:
        conn.execute(statement)
    # One statement: TRUNCATE refuses a table whose FK source is not truncated
    # in the same command (workflow_steps -> workflows).
    conn.execute(
        "TRUNCATE jobs, workflows, workflow_steps, effects, executions, "
        "violations, pardon"
    )

    injected = (
        ["chaos_v2_fail"] * inject_fail
        + ["chaos_v2_nr"] * inject_nr
        + ["chaos_v2"] * (total - inject_fail - inject_nr)
    )
    random.Random(INJECTION_SEED).shuffle(injected)
    for index, wf_type in enumerate(injected):
        workflows.start_workflow(
            conn,
            wf_type=wf_type,
            definition=WORKFLOWS[wf_type],
            input={"index": index},
            dedupe_key=f"chaos-v2-{index}",
            settings=settings,
        )
    for table in ("jobs", "workflows", "workflow_steps"):
        conn.execute(f"ANALYZE {table}")
    print(
        f"created {total} workflows "
        f"(injected {inject_fail} fail, {inject_nr} nr)"
    )

    env = os.environ.copy()
    env.update(
        {
            "ENGINE_HANDLERS_MODULE": HANDLER_MODULE,
            "ENGINE_WORKFLOWS_MODULE": HANDLER_MODULE,
            "ENGINE_LEASE_SECONDS": str(LEASE_SECONDS),
            "ENGINE_REAPER_INTERVAL_SECONDS": "1",
            "ENGINE_RECONCILER_INTERVAL_SECONDS": str(RECONCILER_INTERVAL_SECONDS),
            "ENGINE_BACKOFF_BASE_SECONDS": str(BACKOFF_BASE_SECONDS),
            "ENGINE_BACKOFF_CAP_SECONDS": str(BACKOFF_CAP_SECONDS),
            "ENGINE_IDLE_SLEEP_MIN_SECONDS": "0.05",
            "ENGINE_IDLE_SLEEP_MAX_SECONDS": "0.2",
            "PYTHONPATH": str(_REPO_ROOT / "src"),
        }
    )

    def start_worker(slot: int) -> tuple[subprocess.Popen[Any], str]:
        worker_id = f"chaos-v2-w{slot}-{uuid.uuid4().hex[:8]}"
        proc = subprocess.Popen(
            [sys.executable, "-m", "engine.worker"],
            cwd=_REPO_ROOT,
            env={**env, "ENGINE_WORKER_ID": worker_id},
        )
        return proc, worker_id

    entries: list[tuple[subprocess.Popen[Any], str]] = [
        start_worker(slot) for slot in range(WORKER_COUNT)
    ]
    started = time.monotonic()
    next_kill = started + random.uniform(*KILL_INTERVAL_RANGE)
    kills = 0
    restarts = 0
    kill_log: list[dict[str, Any]] = []
    killed_with_job = 0
    drained = False

    while True:
        now = time.monotonic()
        if now - started > drain_timeout:
            break
        for slot, (proc, _worker_id) in enumerate(entries):
            if proc.poll() is not None:
                entries[slot] = start_worker(slot)
                restarts += 1
        if now >= next_kill:
            alive = [slot for slot, (proc, _) in enumerate(entries) if proc.poll() is None]
            if alive:
                victim = random.choice(alive)
                _proc, worker_id = entries[victim]
                held = _count(
                    conn,
                    "SELECT count(*) AS n FROM jobs "
                    "WHERE state = 'running' AND locked_by = %(w)s",
                    {"w": worker_id},
                )
                entries[victim][0].kill()  # hard kill on Linux (Docker)
                kills += 1
                killed_with_job += 1 if held > 0 else 0
                kill_log.append(
                    {
                        "kill": kills,
                        "slot": victim,
                        "worker_id": worker_id,
                        "held_running_jobs": held,
                    }
                )
                print(
                    f"kill #{kills}: worker={worker_id} "
                    f"held_running_jobs={held}"
                )
                entries[victim] = start_worker(victim)
                restarts += 1
            next_kill = time.monotonic() + random.uniform(*KILL_INTERVAL_RANGE)
        busy = _count(
            conn, "SELECT count(*) AS n FROM jobs WHERE state IN ('queued', 'running')"
        )
        unfinished = _count(
            conn,
            "SELECT count(*) AS n FROM workflows WHERE state NOT IN ('succeeded', 'failed')",
        )
        if busy == 0 and unfinished == 0:
            drained = True
            break
        if int(now - started) % 30 == 0 and int(now - started) > 0:
            print(
                f"t={int(now - started)}s busy_jobs={busy} "
                f"unfinished_workflows={unfinished}"
            )
        time.sleep(1.0)

    duration = round(time.monotonic() - started, 1)

    for proc, _worker_id in entries:
        if proc.poll() is None:
            proc.kill()
    for proc, _worker_id in entries:
        proc.wait(timeout=10)

    jobs_total = _count(conn, "SELECT count(*) AS n FROM jobs")
    jobs_per_second = round(jobs_total / duration, 1) if duration > 0 else 0.0
    workflow_states_rows = conn.execute(
        "SELECT state, count(*) AS n FROM workflows GROUP BY state"
    ).fetchall()
    workflow_states = {str(r["state"]): int(r["n"]) for r in workflow_states_rows}
    job_states_rows = conn.execute(
        "SELECT state, count(*) AS n FROM jobs GROUP BY state"
    ).fetchall()
    job_states = {str(r["state"]): int(r["n"]) for r in job_states_rows}
    histogram_rows = conn.execute(
        "SELECT attempts, count(*) AS n FROM jobs GROUP BY attempts ORDER BY attempts"
    ).fetchall()
    attempts_histogram = {str(r["attempts"]): int(r["n"]) for r in histogram_rows}

    executions_total = _count(conn, "SELECT count(*) AS n FROM executions")
    distinct_effect_keys = _count(
        conn, "SELECT count(DISTINCT effect_key) AS n FROM executions"
    )
    duplicate_executions = executions_total - distinct_effect_keys
    effects_total = _count(conn, "SELECT count(*) AS n FROM effects")
    duplicate_effects = _count(
        conn,
        "SELECT count(*) AS n FROM (SELECT effect_key FROM effects "
        "GROUP BY effect_key HAVING count(*) > 1) d",
    )
    claim_violations = _count(conn, "SELECT count(*) AS n FROM violations")
    effects_incomplete = _count(
        conn,
        "SELECT count(*) AS n FROM workflows wf WHERE wf.state = 'succeeded' AND ("
        " (SELECT count(*) FROM effects e WHERE e.workflow_id = wf.id) <> 4"
        " OR (SELECT count(DISTINCT e.step_name) FROM effects e"
        "     WHERE e.workflow_id = wf.id) <> 4)",
    )
    sums_bad = _sums_chain_bad(conn)
    failed_workflows = workflow_states.get("failed", 0)
    expected_failed = inject_fail + inject_nr
    dead_letter_gaps = _count(conn, _DEAD_LETTER_GAPS_SQL)

    latency_rows = conn.execute(
        """
        SELECT EXTRACT(EPOCH FROM (wf.updated_at - j.updated_at)) AS seconds
        FROM workflows wf
        JOIN jobs j ON j.type = 'workflow_step' AND j.state = 'dead'
             AND j.payload ->> 'workflow_id' = wf.id::text
        WHERE wf.state = 'failed'
        """
    ).fetchall()
    latency_seconds = [float(r["seconds"]) for r in latency_rows]

    # --- replay phase: pardon one failed workflow, replay it, watch it finish
    replay: dict[str, Any] = {"attempted": False}
    victim_wf = conn.execute(
        "SELECT id FROM workflows WHERE state = 'failed' AND type = 'chaos_v2_fail' "
        "ORDER BY id LIMIT 1"
    ).fetchone()
    if victim_wf is not None:
        replay["attempted"] = True
        replay["workflow_id"] = str(victim_wf["id"])
        dead_job = conn.execute(
            "SELECT id FROM jobs WHERE type = 'workflow_step' AND state = 'dead' "
            "AND payload ->> 'workflow_id' = %(wf)s ORDER BY id LIMIT 1",
            {"wf": str(victim_wf["id"])},
        ).fetchone()
        if dead_job is None:
            replay["error"] = "no dead job found for the failed workflow"
        else:
            replay["job_id"] = str(dead_job["id"])
            conn.execute("INSERT INTO pardon (job_id) VALUES (%(j)s)", {"j": dead_job["id"]})
            result = dlq.replay(conn, dead_job["id"], budget=REPLAY_BUDGET)
            replay.update(
                {
                    "replay_budget": REPLAY_BUDGET,
                    "max_attempts_after_replay": result.max_attempts,
                    "replay_count": result.replay_count,
                }
            )
            replay_proc, replay_worker_id = start_worker(WORKER_COUNT)
            replay_started = time.monotonic()
            finished: str | None = None
            deadline = time.monotonic() + REPLAY_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                row = conn.execute(
                    "SELECT state FROM workflows WHERE id = %(id)s",
                    {"id": victim_wf["id"]},
                ).fetchone()
                if row is not None and row["state"] in ("succeeded", "failed"):
                    finished = str(row["state"])
                    break
                time.sleep(0.5)
            replay["finished_state"] = finished
            replay["duration_seconds"] = round(time.monotonic() - replay_started, 1)
            replay_proc.terminate()
            try:
                replay_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                replay_proc.kill()
            replay_job = conn.execute(
                "SELECT state, attempts, max_attempts, replay_count FROM jobs "
                "WHERE id = %(id)s",
                {"id": dead_job["id"]},
            ).fetchone()
            if replay_job is not None:
                replay.update(
                    {
                        "job_state": str(replay_job["state"]),
                        "job_attempts": int(replay_job["attempts"]),
                        "job_replay_count": int(replay_job["replay_count"]),
                    }
                )
            effect_counts = conn.execute(
                "SELECT count(*) AS n, count(DISTINCT step_name) AS d FROM effects "
                "WHERE workflow_id = %(id)s",
                {"id": victim_wf["id"]},
            ).fetchone()
            replay["effects_total"] = int(effect_counts["n"]) if effect_counts else 0
            replay["effects_distinct_steps"] = int(effect_counts["d"]) if effect_counts else 0
            replay["sums_bad"] = _sums_chain_bad(conn, workflow_id=victim_wf["id"])

    postgres_row = conn.execute("SELECT version()").fetchone()

    gate = {
        "drained": drained,
        "all_workflows_terminal": set(workflow_states) <= {"succeeded", "failed"},
        "succeeded_effects_complete": effects_incomplete == 0,
        "succeeded_sums_chain": sums_bad == 0,
        "failed_matches_injected": failed_workflows == expected_failed,
        "failed_in_dead_letters": dead_letter_gaps == 0,
        "no_claim_violations": claim_violations == 0,
        "duplicate_effects_zero": duplicate_effects == 0,
        "replay_succeeds": (
            replay.get("attempted") is True
            and replay.get("finished_state") == "succeeded"
            and replay.get("effects_total") == STEPS_PER_WORKFLOW
            and replay.get("effects_distinct_steps") == STEPS_PER_WORKFLOW
            and replay.get("sums_bad") == 0
        ),
    }

    report: dict[str, Any] = {
        "date": datetime.now(UTC).strftime("%Y-%m-%d"),
        "environment": {
            **_machine_specs(),
            "postgres": str(postgres_row["version"]) if postgres_row else "unknown",
            "database": urlsplit(url).path.lstrip("/"),
        },
        "config": {
            "workflows": total,
            "steps_per_workflow": STEPS_PER_WORKFLOW,
            "delay_step_seconds": DELAY_STEP_SECONDS,
            "injected": {"fail": inject_fail, "nr": inject_nr, "seed": INJECTION_SEED},
            "normal_step_max_attempts": SUM_BUDGET,
            "failing_step_max_attempts": 3,
            "workers": WORKER_COUNT,
            "lease_seconds": LEASE_SECONDS,
            "heartbeat_interval_seconds": round(LEASE_SECONDS / 3, 3),
            "reconciler_interval_seconds": RECONCILER_INTERVAL_SECONDS,
            "backoff_base_seconds": BACKOFF_BASE_SECONDS,
            "backoff_cap_seconds": BACKOFF_CAP_SECONDS,
            "kill_interval_seconds": list(KILL_INTERVAL_RANGE),
            "drain_timeout_seconds": drain_timeout,
        },
        "results": {
            "duration_seconds": duration,
            "jobs_total": jobs_total,
            "jobs_per_second": jobs_per_second,
            "kills": kills,
            "restarts": restarts,
            "kills_with_running_job": killed_with_job,
            "kill_fraction_mid_job": round(killed_with_job / kills, 3) if kills else 0.0,
            "kill_log": kill_log,
            "workflow_states": workflow_states,
            "job_states": job_states,
            "attempts_histogram": attempts_histogram,
            "executions": executions_total,
            "distinct_effect_keys": distinct_effect_keys,
            "duplicate_executions": duplicate_executions,
            "effects_total": effects_total,
            "duplicate_effects": duplicate_effects,
            "claim_violations": claim_violations,
            "reconciler_latency_seconds": _stats(latency_seconds),
            "replay": replay,
        },
        "notes": {
            "duplicate_executions": (
                "count(executions) - count(DISTINCT effect_key): executions of "
                "the same step beyond the first (kill-redeliveries and the "
                "injected failing steps' own retry attempts). This is the "
                "real evidence of the chaos run: redelivered executions "
                "absorbed."
            ),
            "duplicate_effects": (
                "true by construction: effects(effect_key UNIQUE) + ON "
                "CONFLICT DO NOTHING cannot produce more than one row per "
                "key, so this number is not evidence by itself; the evidence "
                "is duplicate_executions (absorbed) plus the one-effect-row-"
                "per-step and sums checks. Idempotency is a handler contract "
                "- the engine's part is the stable effect_key in the step "
                "context."
            ),
            "replay_phase_test_hook": (
                "the replayed workflow's always-failing step was made to "
                "succeed via a pardon scaffolding row before dlq.replay ran; "
                "in real use replay only helps once the underlying cause is "
                "fixed."
            ),
            "reconciler_latency": (
                "workflows.updated_at - jobs.updated_at for failed workflows "
                "(both DB-clock stamps at their transitions); bounded by the "
                "reconciler cadence (see config.reconciler_interval_seconds) "
                "by design. Measured before the replay phase."
            ),
            "throughput": (
                "jobs_per_second is a chaos-run figure for THIS environment "
                "and run (workflow steps, 20-60 ms handler sleeps, 2 s timer "
                "steps, kill/restart cycles included) - a chaos-run "
                "measurement, not a capacity number and not a database "
                "benchmark."
            ),
        },
        "gate": gate,
    }

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"chaos-v2-{datetime.now(UTC).strftime('%Y%m%d')}.json"
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"report saved to {path}")

    if not drained:
        print("FAILED: workflows did not drain within the timeout")
        return 1
    failed_gates = [name for name, ok in gate.items() if not ok]
    if failed_gates:
        print(f"FAILED: gate not satisfied: {failed_gates}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
