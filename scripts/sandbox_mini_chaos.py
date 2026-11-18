"""Sandbox mini-chaos (checkpoint 9, task 5): the kill loop against REAL
sandboxes — 200 sandbox-test-run workflows, 3 workers, hard kills every 3-6 s.

Run from the HOST python (Windows is fine here: the script only
TerminateProcess'es its own worker children — the kill demo proved the
mechanics; everything inside the engine is daemon-side):

    DATABASE_URL=postgresql://app:app@localhost:5433/app_test \
      python scripts/sandbox_mini_chaos.py

Setup: ONE shared repository snapshot blob (requirements.txt = the hashed
six pin, src/main.py = a fast payload), 200 workflows started with the same
input; 3 worker subprocesses run the REAL Docker sandbox pipeline
(cached install -> hardened container -> sandbox_runs record).

Kill loop: a random worker is hard-killed every 3-6 s and replaced (the
kill demo showed one such cycle end to end). A killed worker's in-flight
step job waits for its lease to expire (~2 s), then a retry takes over —
the retry's run step removes earlier attempts' containers first.

Step budget: a murdered job costs an attempt like any other failure, so
the kill loop needs a retry budget sized to outlive the bombardment. Run
2 (authored budget 5) dead-lettered 42 workflows after 428 mid-job kills
— correct engine behaviour, wrong budget for the experiment (preserved
as sandbox-mini-chaos-20261118-budget5-42failures.json). Run 3 sets
ENGINE_SANDBOX_STEP_BUDGET=40, applied by the SHARED handlers module this
script imports; the worker subprocesses inherit the same env, so every
definition snapshot — worker-side and caller-side — carries the budget.

Gates (ALL required, checked after drain):
1. every workflow terminal, and ALL succeeded (no injected failures here —
   a kill must cost time, not correctness);
2. zero `engine-sandbox` containers left after a final janitor pass;
3. no duplicate (job_id, attempt) in sandbox_runs — the UNIQUE constraint's
   territory; the query is belt-and-braces evidence;
4. exactly one sandbox_runs row per succeeded workflow (the committing
   attempt's row; killed attempts leave NO row — measurements, not effects);
5. the A->B->A->B regression tests (tests/test_workflows.py:
   test_crash_mid_step_resumes_at_that_step, test_committed_step_is_not_
   rerun) pass via pytest subprocess.

The report lands in docs/results/sandbox-mini-chaos-<date>.json.
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
from statistics import median
from typing import Any
from urllib.parse import urlsplit

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

# set BEFORE the handlers module import: the module builds its WORKFLOWS at
# import time from the same env knob, so the workers' definitions and this
# script's start_workflow snapshot agree on the budget.
STEP_BUDGET = 40
os.environ["ENGINE_SANDBOX_STEP_BUDGET"] = str(STEP_BUDGET)

import docker  # noqa: E402
import sandbox_demo_handlers  # noqa: E402

from engine import db, patchlog  # noqa: E402
from engine.config import load_settings  # noqa: E402
from engine.sandbox import (  # noqa: E402
    DockerSandboxAdapter,
    build_workspace_tar,
    sweep_containers,
)
from engine.sandbox_workflow import WORKFLOW_TYPE  # noqa: E402
from engine.workflows import start_workflow  # noqa: E402

TOTAL_WORKFLOWS = 200
WORKER_COUNT = 3
KILL_INTERVAL_RANGE = (3.0, 6.0)
# sized by measurement, twice: the FIRST run (lease 4 s, reaper 1 s, 20-min
# timeout, preserved as sandbox-mini-chaos-20261118-lease4-nondrained.json)
# showed the machinery fully correct (0 failed, max step attempts 2, zero
# leftovers) but did not drain in 20 min: 200 in-flight workflows over 3
# workers put each workflow's 4 sequential jobs behind ~200 others (Little's
# law), and every murder costs a lease-expiry wait. Halving the lease,
# speeding the reaper, and sizing the timeout for the measured service rate
# got run 2 to drain (2210.7 s) — but run 2's 42 dead-lettered workflows also
# LEFT the queue early, relieving it. Run 3 (budget 40, no early exits,
# preserved as ...-budget40-nondrained.json) held every invariant with max
# step attempts 8 and 175/200 done at 2400 s — the same ~0.3 useful jobs/s
# now has to carry ALL 800 jobs plus every murder-wasted attempt. The cap is
# generous on purpose: the drain check exits the moment busy==0, so a cap
# that is never reached costs nothing.
DRAIN_TIMEOUT_SECONDS = 4500.0
LEASE_SECONDS = 2.0
REAPER_INTERVAL_SECONDS = 0.5
RECONCILER_INTERVAL_SECONDS = 1.0
BACKOFF_BASE_SECONDS = 0.2
BACKOFF_CAP_SECONDS = 5.0
RESULTS_DIR = _REPO_ROOT / "docs" / "results"
HANDLER_MODULE = str(_REPO_ROOT / "scripts" / "sandbox_demo_handlers.py")
_SIX_HASH = "8abb2f1d86890a2dfb989f9a77cfcfd3e47c2a354b01111771326f8aa26e0254"
_PINS = f"six==1.16.0 --hash=sha256:{_SIX_HASH}\n".encode()
_COMMAND = ("python", "src/main.py")
_PAYLOAD = b"import sys\nprint('sandbox-ok')\n"

# the two A->B->A->B regression tests (checkpoint 2 machinery, still the
# contract every workflow advance relies on)
REGRESSION_TESTS = [
    "tests/test_workflows.py::test_crash_mid_step_resumes_at_that_step",
    "tests/test_workflows.py::test_committed_step_is_not_rerun",
]


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise SystemExit("DATABASE_URL is required (mini-chaos refuses defaults)")
    dbname = urlsplit(url).path.lstrip("/")
    if not dbname.endswith("_test"):
        raise SystemExit(
            f"refusing to run mini-chaos against database {dbname!r}: the name "
            "must end with '_test'"
        )
    return url


def _count(conn: Any, sql: str, params: dict[str, Any] | None = None) -> int:
    row = conn.execute(sql, params or {}).fetchone()
    return int(row["n"]) if row is not None else 0


def _machine_specs() -> dict[str, Any]:
    spec: dict[str, Any] = {
        "runner": "host python (worker children are the script's own; "
        "engine state is daemon-side)",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpus": os.cpu_count(),
    }
    try:
        info = docker.from_env().info()
        spec["docker_version"] = info.get("ServerVersion")
        spec["docker_mem_total_gb"] = round((info.get("MemTotal") or 0) / 1024**3, 2)
        spec["docker_ncpu"] = info.get("NCPU")
    except Exception:  # pragma: no cover - recorded as None on failure
        pass
    return spec


def _leftover_containers() -> list[str]:
    client = docker.from_env()
    return [
        str(c.id)[:12]
        for c in client.containers.list(all=True, filters={"label": "engine-sandbox"})
    ]


def _stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "min_seconds": round(ordered[0], 1),
        "p50_seconds": round(median(ordered), 1),
        "p95_seconds": round(ordered[max(0, int(0.95 * (len(ordered) - 1)))], 1),
        "max_seconds": round(ordered[-1], 1),
    }


def _run_regression_tests(report: dict[str, Any]) -> bool:
    env = os.environ.copy()
    env["DATABASE_URL"] = _database_url()
    env["PYTHONPATH"] = str(_REPO_ROOT / "src")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *REGRESSION_TESTS, "-q", "--no-header"],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    tail = (proc.stdout or "").strip().splitlines()[-3:]
    print(f"OBSERVED regression tests: rc={proc.returncode} :: {tail}")
    report["regression_tests"] = {
        "tests": REGRESSION_TESTS,
        "returncode": proc.returncode,
        "tail": tail,
    }
    return proc.returncode == 0


def main() -> int:
    url = _database_url()
    report: dict[str, Any] = {
        "date": datetime.now(UTC).strftime("%Y-%m-%d"),
        "environment": _machine_specs(),
        "total_workflows": TOTAL_WORKFLOWS,
        "worker_count": WORKER_COUNT,
        "kill_interval_range_s": list(KILL_INTERVAL_RANGE),
        "lease_seconds": LEASE_SECONDS,
        "reaper_interval_seconds": REAPER_INTERVAL_SECONDS,
        "step_budget": STEP_BUDGET,
    }
    # FIRST: the A->B->A->B regression tests. They run under the pytest
    # fixture that truncates the test DB, so they must not run after the
    # chaos data is on the floor.
    regression_ok = _run_regression_tests(report)

    settings = replace(
        load_settings(),
        database_url=url,
        lease_seconds=LEASE_SECONDS,
        heartbeat_interval_seconds=LEASE_SECONDS / 3,
        reaper_interval_seconds=REAPER_INTERVAL_SECONDS,
        reconciler_interval_seconds=RECONCILER_INTERVAL_SECONDS,
        backoff_base_seconds=BACKOFF_BASE_SECONDS,
        backoff_cap_seconds=BACKOFF_CAP_SECONDS,
        idle_sleep_min_seconds=0.05,
        idle_sleep_max_seconds=0.2,
    )
    conn = db.connect(settings, application_name="engine-sandbox-mini-chaos")

    # one shared snapshot: same pins + payload for all 200 workflows
    snapshot_hash = patchlog.store_blob(
        conn, build_workspace_tar({"requirements.txt": _PINS, "src/main.py": _PAYLOAD}),
        cap_bytes=32 * 1024 * 1024,
    )
    definition = sandbox_demo_handlers.build_definition(
        DockerSandboxAdapter(docker.from_env()),
        settings=settings,
        allowed_commands=(_COMMAND,),
    )
    conn.execute(
        "TRUNCATE jobs, workflows, workflow_steps, sandbox_runs, tool_calls"
    )
    started_at = time.monotonic()
    for index in range(TOTAL_WORKFLOWS):
        start_workflow(
            conn,
            wf_type=WORKFLOW_TYPE,
            definition=definition,
            input={"snapshot_hash": snapshot_hash, "command": list(_COMMAND)},
        )
        if (index + 1) % 50 == 0:
            print(f"created {index + 1}/{TOTAL_WORKFLOWS} workflows")
    conn.execute("ANALYZE jobs, workflows, workflow_steps")

    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": url,
            "ENGINE_HANDLERS_MODULE": HANDLER_MODULE,
            "ENGINE_WORKFLOWS_MODULE": HANDLER_MODULE,
            "ENGINE_LEASE_SECONDS": str(LEASE_SECONDS),
            "ENGINE_REAPER_INTERVAL_SECONDS": str(REAPER_INTERVAL_SECONDS),
            "ENGINE_SANDBOX_STEP_BUDGET": str(STEP_BUDGET),
            "ENGINE_RECONCILER_INTERVAL_SECONDS": str(RECONCILER_INTERVAL_SECONDS),
            "ENGINE_BACKOFF_BASE_SECONDS": str(BACKOFF_BASE_SECONDS),
            "ENGINE_BACKOFF_CAP_SECONDS": str(BACKOFF_CAP_SECONDS),
            "ENGINE_IDLE_SLEEP_MIN_SECONDS": "0.05",
            "ENGINE_IDLE_SLEEP_MAX_SECONDS": "0.2",
            "PYTHONPATH": str(_REPO_ROOT / "src"),
        }
    )

    def start_worker(slot: int) -> tuple[subprocess.Popen[Any], str]:
        worker_id = f"mini-chaos-w{slot}-{uuid.uuid4().hex[:8]}"
        proc = subprocess.Popen(
            [sys.executable, "-m", "engine.worker"],
            cwd=_REPO_ROOT,
            env={**env, "ENGINE_WORKER_ID": worker_id},
        )
        return proc, worker_id

    entries: list[tuple[subprocess.Popen[Any], str]] = [
        start_worker(slot) for slot in range(WORKER_COUNT)
    ]
    next_kill = time.monotonic() + random.uniform(*KILL_INTERVAL_RANGE)
    kills = 0
    restarts = 0
    killed_with_job = 0
    kill_log: list[dict[str, Any]] = []
    drained = False

    while True:
        now = time.monotonic()
        elapsed = now - started_at
        if elapsed > DRAIN_TIMEOUT_SECONDS:
            break
        for slot, (proc, _worker_id) in enumerate(entries):
            if proc.poll() is not None:  # died on its own: replace
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
                entries[victim][0].kill()  # hard kill (TerminateProcess on Windows)
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
                print(f"kill #{kills}: worker={worker_id} held_running_jobs={held}")
                entries[victim] = start_worker(victim)
                restarts += 1
            next_kill = time.monotonic() + random.uniform(*KILL_INTERVAL_RANGE)
        busy = _count(conn, "SELECT count(*) AS n FROM jobs WHERE state IN ('queued', 'running')")
        unfinished = _count(
            conn,
            "SELECT count(*) AS n FROM workflows WHERE state NOT IN ('succeeded', 'failed')",
        )
        if busy == 0 and unfinished == 0:
            drained = True
            break
        if int(elapsed) % 30 == 0 and int(elapsed) > 0:
            rows = conn.execute(
                "SELECT state, count(*) AS n FROM workflows GROUP BY state"
            ).fetchall()
            states = {str(r["state"]): int(r["n"]) for r in rows}
            print(
                f"t={int(elapsed)}s busy_jobs={busy} "
                f"unfinished_workflows={unfinished} states={states}"
            )
        time.sleep(1.0)

    duration = round(time.monotonic() - started_at, 1)
    for proc, _worker_id in entries:
        if proc.poll() is None:
            proc.kill()
    for proc, _worker_id in entries:
        proc.wait(timeout=10)

    # ---- gates ------------------------------------------------------------
    workflow_states_rows = conn.execute(
        "SELECT state, count(*) AS n FROM workflows GROUP BY state"
    ).fetchall()
    workflow_states = {str(r["state"]): int(r["n"]) for r in workflow_states_rows}
    failed = workflow_states.get("failed", 0)
    succeeded = workflow_states.get("succeeded", 0)
    terminal = succeeded + failed == TOTAL_WORKFLOWS

    duplicate_run_rows = _count(
        conn,
        "SELECT count(*) AS n FROM (SELECT job_id, attempt FROM sandbox_runs"
        " GROUP BY job_id, attempt HAVING count(*) > 1) d",
    )
    run_rows_total = _count(conn, "SELECT count(*) AS n FROM sandbox_runs")
    runs_with_no_workflow = _count(
        conn,
        "SELECT count(*) AS n FROM sandbox_runs r"
        " LEFT JOIN workflows wf ON wf.id = r.workflow_id"
        " WHERE wf.id IS NULL",
    )
    attempt_hist_rows = conn.execute(
        "SELECT attempts, count(*) AS n FROM jobs GROUP BY attempts ORDER BY attempts"
    ).fetchall()
    attempts_histogram = {str(r["attempts"]): int(r["n"]) for r in attempt_hist_rows}
    max_step_attempts = conn.execute(
        "SELECT COALESCE(max(attempts), 0) AS m FROM jobs"
    ).fetchone()["m"]

    durations = [
        float(r["seconds"])
        for r in conn.execute(
            "SELECT EXTRACT(EPOCH FROM (updated_at - created_at)) AS seconds"
            " FROM workflows WHERE state = 'succeeded'"
        ).fetchall()
    ]
    conn.close()

    adapter = DockerSandboxAdapter(docker.from_env())
    janitor_started = time.monotonic()
    sweep = sweep_containers(adapter, max_age_seconds=0.0)
    janitor_seconds = round(time.monotonic() - janitor_started, 3)
    leftovers = _leftover_containers()

    report.update(
        {
            "drained": drained,
            "duration_seconds": duration,
            "kills": kills,
            "killed_with_running_job": killed_with_job,
            "restarts": restarts,
            "kill_log": kill_log[-10:],
            "workflow_states": workflow_states,
            "attempts_histogram": attempts_histogram,
            "max_step_attempts": int(max_step_attempts),
            "workflow_duration_seconds": _stats(durations),
            "sandbox_runs_rows": run_rows_total,
            "duplicate_run_rows": duplicate_run_rows,
            "runs_with_no_workflow": runs_with_no_workflow,
            "janitor": {
                "removed": sweep.removed,
                "failed": sweep.failed,
                "pass_seconds": janitor_seconds,
                "leftover_after_pass": leftovers,
            },
        }
    )

    gates = {
        "drained_in_time": drained,
        "all_workflows_terminal": terminal,
        "all_workflows_succeeded": succeeded == TOTAL_WORKFLOWS and failed == 0,
        "zero_leftover_containers": not leftovers,
        "no_duplicate_run_rows": duplicate_run_rows == 0,
        "one_run_row_per_workflow": run_rows_total == succeeded,
        "no_orphan_run_rows": runs_with_no_workflow == 0,
        "regression_tests_pass": regression_ok,
    }
    report["gates"] = gates
    for name, passed in gates.items():
        print(f"GATE {'PASS' if passed else 'FAIL'}: {name}")
    print(
        f"OBSERVED mini-chaos: {TOTAL_WORKFLOWS} workflows in {duration}s "
        f"({round(TOTAL_WORKFLOWS / duration, 2) if duration else 0} wf/s), "
        f"{kills} kills ({killed_with_job} with a running job), "
        f"{succeeded} succeeded, {failed} failed, "
        f"max step attempts={max_step_attempts}, "
        f"sandbox_runs rows={run_rows_total}, "
        f"janitor removed={sweep.removed} leftovers={leftovers}"
    )

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"sandbox-mini-chaos-{datetime.now(UTC).strftime('%Y%m%d')}.json"
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"report saved to {path}")
    if not all(gates.values()):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
