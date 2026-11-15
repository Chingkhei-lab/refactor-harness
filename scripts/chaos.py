"""Chaos exit check: hard-kill workers mid-flight and verify no job is lost.

Run on Linux (Docker or WSL2) — Windows cannot signal processes reliably:

    docker run --rm -v "$PWD":/app -w /app \
      -e DATABASE_URL=postgresql://app:app@host.docker.internal:5433/app_test \
      python:3.12-slim \
      bash -c "pip install -q 'psycopg[binary]' alembic && python scripts/chaos.py"

What it does: creates side_effects/violations (test scaffolding, outside
Alembic), bulk-enqueues 10,000 jobs, runs ANALYZE, starts 3 worker processes,
hard-kills (proc.kill()) a random worker every 3-6 s and starts a replacement
immediately, then waits for the queue to drain (10-minute timeout = failure).

Gate: every job is succeeded or dead; violations is empty; every succeeded
job has a side_effects row with attempt = jobs.attempts. The report lands in
docs/results/chaos-<date>.json.
"""

from __future__ import annotations

import json
import os
import platform
import random
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from engine import db  # noqa: E402
from engine.config import load_settings  # noqa: E402

TOTAL_JOBS = 10_000
WORKER_COUNT = 3
KILL_INTERVAL_RANGE = (3.0, 6.0)
DRAIN_TIMEOUT_SECONDS = 600.0
HANDLER_SLEEP_RANGE_MS = (5, 30)
RESULTS_DIR = _REPO_ROOT / "docs" / "results"

_SCAFFOLD_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS side_effects (
        job_id UUID NOT NULL,
        attempt INTEGER NOT NULL,
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
]


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


def _count(conn: Any, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    return int(row["n"]) if row is not None else 0


def main() -> int:
    url = _database_url()
    os.environ["ALEMBIC_DATABASE_URL"] = url
    alembic_config = Config(str(_REPO_ROOT / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    command.upgrade(alembic_config, "head")

    lease = float(os.environ.get("CHAOS_LEASE_SECONDS", "4"))
    settings = replace(
        load_settings(),
        database_url=url,
        lease_seconds=lease,
        heartbeat_interval_seconds=lease / 3,
        reaper_interval_seconds=1.0,
        idle_sleep_min_seconds=0.05,
        idle_sleep_max_seconds=0.2,
    )
    conn = db.connect(settings, application_name="engine-chaos")

    for statement in _SCAFFOLD_TABLES_SQL:
        conn.execute(statement)
    for table in ("jobs", "side_effects", "violations"):
        conn.execute(f"TRUNCATE {table}")
    conn.execute(
        "INSERT INTO jobs (type) SELECT 'chaos' FROM generate_series(1, %s)",
        (TOTAL_JOBS,),
    )
    conn.execute("ANALYZE jobs")

    env = os.environ.copy()
    env.update(
        {
            "ENGINE_HANDLERS_MODULE": str(_REPO_ROOT / "scripts" / "chaos_handlers.py"),
            "ENGINE_LEASE_SECONDS": str(lease),
            "ENGINE_REAPER_INTERVAL_SECONDS": "1",
            "ENGINE_IDLE_SLEEP_MIN_SECONDS": "0.05",
            "ENGINE_IDLE_SLEEP_MAX_SECONDS": "0.2",
            "PYTHONPATH": str(_REPO_ROOT / "src"),
        }
    )

    def start_worker() -> subprocess.Popen[Any]:
        return subprocess.Popen(
            [sys.executable, "-m", "engine.worker"], cwd=_REPO_ROOT, env=env
        )

    procs = [start_worker() for _ in range(WORKER_COUNT)]
    started = time.monotonic()
    next_kill = started + random.uniform(*KILL_INTERVAL_RANGE)
    kills = 0
    restarts = 0
    drained = False

    while True:
        now = time.monotonic()
        if now - started > DRAIN_TIMEOUT_SECONDS:
            break
        for index, proc in enumerate(procs):
            if proc.poll() is not None:
                procs[index] = start_worker()
                restarts += 1
        if now >= next_kill:
            alive = [index for index, proc in enumerate(procs) if proc.poll() is None]
            if alive:
                victim = random.choice(alive)
                procs[victim].kill()  # hard kill on both Windows and Linux
                kills += 1
                procs[victim] = start_worker()
                restarts += 1
            next_kill = time.monotonic() + random.uniform(*KILL_INTERVAL_RANGE)
        row = conn.execute(
            "SELECT count(*) AS n FROM jobs WHERE state IN ('queued', 'running')"
        ).fetchone()
        if row is not None and row["n"] == 0:
            drained = True
            break
        time.sleep(1.0)

    duration = round(time.monotonic() - started, 1)
    jobs_per_second = round(TOTAL_JOBS / duration, 1) if duration > 0 else 0.0

    states_rows = conn.execute(
        "SELECT state, count(*) AS n FROM jobs GROUP BY state"
    ).fetchall()
    states = {str(row["state"]): int(row["n"]) for row in states_rows}
    histogram_rows = conn.execute(
        "SELECT attempts, count(*) AS n FROM jobs GROUP BY attempts ORDER BY attempts"
    ).fetchall()
    histogram = {str(row["attempts"]): int(row["n"]) for row in histogram_rows}
    violations = _count(conn, "SELECT count(*) AS n FROM violations")
    missing_effects = _count(
        conn,
        "SELECT count(*) AS n FROM jobs j WHERE j.state = 'succeeded' AND NOT EXISTS "
        "(SELECT 1 FROM side_effects s WHERE s.job_id = j.id AND s.attempt = j.attempts)",
    )
    duplicate_effects = _count(
        conn,
        "SELECT count(*) AS n FROM (SELECT job_id FROM side_effects "
        "GROUP BY job_id HAVING count(*) > 1) d",
    )
    version_row = conn.execute("SELECT version()").fetchone()
    postgres_version = str(version_row["version"]) if version_row else "unknown"

    for proc in procs:
        if proc.poll() is None:
            proc.kill()
    for proc in procs:
        proc.wait(timeout=10)

    all_terminal = set(states) <= {"succeeded", "dead"}
    gate = {
        "all_terminal": all_terminal,
        "violations_empty": violations == 0,
        "effects_match_attempts": missing_effects == 0,
        "drained": drained,
    }
    report: dict[str, Any] = {
        "date": datetime.now(UTC).strftime("%Y-%m-%d"),
        "environment": {
            **_machine_specs(),
            "postgres": postgres_version,
            "database": urlsplit(url).path.lstrip("/"),
        },
        "config": {
            "jobs": TOTAL_JOBS,
            "workers": WORKER_COUNT,
            "lease_seconds": lease,
            "reaper_interval_seconds": 1.0,
            "kill_interval_seconds": list(KILL_INTERVAL_RANGE),
            "handler_sleep_ms": list(HANDLER_SLEEP_RANGE_MS),
            "max_attempts": settings.max_attempts,
            "drain_timeout_seconds": DRAIN_TIMEOUT_SECONDS,
        },
        "results": {
            "duration_seconds": duration,
            "jobs_per_second": jobs_per_second,
            "kills": kills,
            "restarts": restarts,
            "succeeded": states.get("succeeded", 0),
            "dead": states.get("dead", 0),
            "attempts_histogram": histogram,
            "duplicate_effects": duplicate_effects,
            "violations": violations,
            "missing_side_effects": missing_effects,
        },
        "gate": gate,
    }

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"chaos-{datetime.now(UTC).strftime('%Y%m%d')}.json"
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"report saved to {path}")

    if not drained:
        print("FAILED: queue did not drain within the timeout")
        return 1
    if not all_terminal or violations > 0 or missing_effects > 0:
        print("FAILED: gate not satisfied")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
