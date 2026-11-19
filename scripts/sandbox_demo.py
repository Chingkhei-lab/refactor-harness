"""Sandbox measurement demo (checkpoint 9, tasks 3/4/6): real Docker.

Runs from the HOST python against Docker Desktop (Windows is fine here: the
script only TerminateProcess'es its own worker children; everything inside
the engine is daemon-side):

    python scripts/sandbox_demo.py

Sections, all writing OBSERVED lines and one JSON report into
docs/results/sandbox-<date>.json:

- kill_demo (task 3): one sandbox-test-run workflow; the worker is killed
  mid-run; the orphan container is shown; a janitor pass removes it; the
  retry finishes the workflow; zero containers remain. Timings recorded.
- install_cold_cached: the dependency image built once (cold) then reused
  (cached), same pins, base digest pinned.
- container_start: 30 create+start-to-running timings, p50/p95.
- copy_in_out: the exec'd-tar transport timed by workspace size.
- concurrency_ramp (task 4): N simultaneous real sandboxes at 256 MiB,
  ramping until the host refuses — memory caps before CPU on 4 CPUs.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

import docker  # noqa: E402
from demo_db import resolve_demo_url  # noqa: E402

from engine import db, install, patchlog  # noqa: E402
from engine.config import load_settings  # noqa: E402
from engine.sandbox import (  # noqa: E402
    DockerSandboxAdapter,
    SandboxLimits,
    SandboxSpec,
    build_workspace_tar,
    copy_in,
    copy_out,
    create_kwargs,
    sweep_containers,
)
from engine.sandbox import (  # noqa: E402
    run as run_sandbox,
)
from engine.sandbox_workflow import WORKFLOW_TYPE, make_sandbox_test_run_workflow  # noqa: E402
from engine.workflows import start_workflow  # noqa: E402

RESULTS_DIR = _REPO_ROOT / "docs" / "results"
BASE_IMAGE = "python:3.12-slim"
_SIX_HASH = "8abb2f1d86890a2dfb989f9a77cfcfd3e47c2a354b01111771326f8aa26e0254"
_PINS = f"six==1.16.0 --hash=sha256:{_SIX_HASH}\n".encode()
_DEMO_COMMAND = ("python", "src/main.py")


def _database_url() -> str:
    # A demo writes evidence; pytest truncates the test database. The rule
    # lives in scripts/demo_db.py so every demo shares it.
    return resolve_demo_url("sandbox_demo")


def _machine_specs() -> dict[str, Any]:
    spec: dict[str, Any] = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpus": os.cpu_count(),
    }
    try:
        info = _docker().info()
        spec["docker_version"] = info.get("ServerVersion")
        spec["docker_mem_total_gb"] = round((info.get("MemTotal") or 0) / 1024**3, 2)
        spec["docker_ncpu"] = info.get("NCPU")
    except Exception:  # pragma: no cover
        pass
    return spec


def _docker() -> Any:
    global _DOCKER_CLIENT
    if _DOCKER_CLIENT is None:
        _DOCKER_CLIENT = docker.from_env()
        _DOCKER_CLIENT.ping()
    return _DOCKER_CLIENT


_DOCKER_CLIENT: Any = None


def _count_leftovers(client: Any, job_label: str | None = None) -> list[str]:
    filters = {"label": "engine-sandbox"}
    if job_label:
        filters = {"label": f"engine-sandbox.job={job_label}"}
    return [str(c.id)[:12] for c in client.containers.list(all=True, filters=filters)]


# ---------------------------------------------------------------------------
# section 1: the kill demo (task 3)
# ---------------------------------------------------------------------------


def _kill_demo(report: dict[str, Any]) -> None:
    settings = replace(
        load_settings(),
        database_url=_database_url(),
        lease_seconds=4.0,
        heartbeat_interval_seconds=1.3,
        reaper_interval_seconds=1.0,
        idle_sleep_min_seconds=0.05,
        idle_sleep_max_seconds=0.2,
    )
    os.environ["DATABASE_URL"] = settings.database_url
    env = os.environ.copy()
    env.update(
        {
            "ENGINE_HANDLERS_MODULE": str(_REPO_ROOT / "scripts" / "sandbox_demo_handlers.py"),
            "ENGINE_WORKFLOWS_MODULE": str(_REPO_ROOT / "scripts" / "sandbox_demo_handlers.py"),
            "ENGINE_LEASE_SECONDS": "4",
            "ENGINE_REAPER_INTERVAL_SECONDS": "1",
            "ENGINE_IDLE_SLEEP_MIN_SECONDS": "0.05",
            "ENGINE_IDLE_SLEEP_MAX_SECONDS": "0.2",
            "PYTHONPATH": str(_REPO_ROOT / "src"),
        }
    )

    conn = db.connect(settings, application_name="engine-demo")
    files = {"requirements.txt": _PINS, "src/main.py": b"import time\ntime.sleep(20)\n"}
    snapshot_hash = patchlog.store_blob(
        conn, build_workspace_tar(files), cap_bytes=32 * 1024 * 1024
    )
    definition = make_sandbox_test_run_workflow(
        DockerSandboxAdapter(_docker()), settings=settings, allowed_commands=(_DEMO_COMMAND,)
    )
    wf = start_workflow(
        conn,
        wf_type=WORKFLOW_TYPE,
        definition=definition,
        input={"snapshot_hash": snapshot_hash, "command": list(_DEMO_COMMAND)},
    )
    print(f"OBSERVED workflow started: {wf.id}")

    def worker() -> subprocess.Popen[Any]:
        return subprocess.Popen([sys.executable, "-m", "engine.worker"], cwd=_REPO_ROOT, env=env)

    workers = [worker(), worker()]
    adapter = DockerSandboxAdapter(_docker())

    # wait until a RUN container is alive (the long-sleeping payload)
    def sandbox_alive() -> bool:
        return any(
            c.attrs.get("State", {}).get("Running")
            for c in _docker().containers.list(all=True, filters={"label": "engine-sandbox"})
        )

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and not sandbox_alive():
        time.sleep(0.5)
    if not sandbox_alive():
        raise SystemExit("kill demo: the run container never started")

    for proc in workers:  # hard-kill BOTH workers: no graceful shutdown path
        proc.kill()
    for proc in workers:
        proc.wait(timeout=10)
    t_kill = time.monotonic()

    orphans = _count_leftovers(_docker())
    print(f"OBSERVED orphan containers after the kill: {len(orphans)} {orphans}")
    assert orphans, "expected the killed run's container to be orphaned"

    # the janitor is age-based: wait until the orphan is older than 6 s
    time.sleep(6.5)
    t_janitor_start = time.monotonic()
    sweep = sweep_containers(adapter, max_age_seconds=5.0)
    t_janitor = round(time.monotonic() - t_janitor_start, 3)
    left_after_janitor = _count_leftovers(_docker())
    print(
        f"OBSERVED janitor pass: removed={sweep.removed} failed={sweep.failed} "
        f"took={t_janitor}s leftovers={left_after_janitor}"
    )

    # restart: the reaper requeues the expired lease; the retry finishes
    workers = [worker(), worker()]
    state = ""
    run_rows: list[dict[str, Any]] = []
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        row = conn.execute("SELECT state FROM workflows WHERE id = %s", (wf.id,)).fetchone()
        state = str(row["state"]) if row else ""
        if state == "succeeded":
            break
        time.sleep(0.5)
    t_finish = round(time.monotonic() - t_kill, 1)
    for r in conn.execute(
        "SELECT attempt, exit_code, image FROM sandbox_runs"
        " WHERE workflow_id = %s ORDER BY attempt",
        (wf.id,),
    ).fetchall():
        run_rows.append({"attempt": int(r["attempt"]), "exit_code": r["exit_code"]})
    leftovers = _count_leftovers(_docker())
    for proc in workers:
        proc.kill()
        proc.wait(timeout=10)
    conn.close()

    print(
        f"OBSERVED workflow {state} {t_finish}s after the kill; "
        f"sandbox_runs={run_rows} leftovers={leftovers}"
    )
    report["kill_demo"] = {
        "orphan_containers_after_kill": len(orphans),
        "janitor_removed": sweep.removed,
        "janitor_failed": sweep.failed,
        "janitor_pass_seconds": t_janitor,
        "workflow_state_after_restart": state,
        "seconds_from_kill_to_success": t_finish,
        "sandbox_runs_rows": run_rows,
        "leftover_containers_at_end": len(leftovers),
        "gate": {
            "orphan_shown": len(orphans) >= 1,
            "janitor_removed_orphan": sweep.removed >= 1,
            "workflow_succeeded": state == "succeeded",
            "zero_containers_left": len(leftovers) == 0,
        },
    }
    if state != "succeeded" or leftovers:
        raise SystemExit("kill demo FAILED its gate")


# ---------------------------------------------------------------------------
# section 2: install cold vs cached
# ---------------------------------------------------------------------------


def _install_cold_cached(report: dict[str, Any]) -> None:
    adapter = DockerSandboxAdapter(_docker())
    base_digest = _base_digest(adapter)
    key = install.install_key(_PINS, base_digest)
    try:
        _docker().images.remove(f"engine-deps:{key}", force=True)
    except Exception:
        pass
    cold = install.ensure_dependency_image(
        adapter,
        requirements=_PINS,
        base_image=BASE_IMAGE,
        job_id="demo-install",
        attempt=1,
    )
    warm = install.ensure_dependency_image(
        adapter,
        requirements=_PINS,
        base_image=BASE_IMAGE,
        job_id="demo-install",
        attempt=1,
    )
    print(
        f"OBSERVED install cold={cold.duration_seconds}s (cached={cold.cached}) "
        f"cached={warm.duration_seconds}s (cached={warm.cached}) "
        f"key={key[:12]} image={cold.image[:19]}"
    )
    report["install_cold_cached"] = {
        "install_key": key,
        "image_id": cold.image,
        "base_digest": base_digest,
        "cold_seconds": cold.duration_seconds,
        "cold_was_cached": cold.cached,
        "cached_seconds": warm.duration_seconds,
        "cached_was_cached": warm.cached,
        "same_image_id": cold.image == warm.image,
    }


def _base_digest(adapter: Any) -> str:
    attrs = adapter.image_attrs(BASE_IMAGE)
    assert attrs is not None and attrs.get("RepoDigests"), "no RepoDigests on base"
    return str(attrs["RepoDigests"][0]).split("@", 1)[1].removeprefix("sha256:")


# ---------------------------------------------------------------------------
# section 3: container start p50/p95
# ---------------------------------------------------------------------------


def _container_start(report: dict[str, Any]) -> None:
    client = _docker()
    image = _base_image_id()
    durations: list[float] = []
    for _ in range(30):
        container = client.containers.create(
            image=image,
            command=["python", "-c", "pass"],
            network_mode="none",
        )
        try:
            began = time.monotonic()
            container.start()
            while True:
                container.reload()
                if container.attrs.get("State", {}).get("Running"):
                    break
                if container.attrs.get("State", {}).get("Status") == "exited":
                    break
                time.sleep(0.002)
            durations.append(round(time.monotonic() - began, 4))
        finally:
            container.remove(force=True)
    durations.sort()

    def pct(p: float) -> float:
        return durations[min(len(durations) - 1, int(p * len(durations)))]

    print(
        f"OBSERVED container start p50={pct(0.5):.3f}s p95={pct(0.95):.3f}s "
        f"over {len(durations)} starts (min={durations[0]:.3f} max={durations[-1]:.3f})"
    )
    report["container_start"] = {
        "runs": len(durations),
        "p50_seconds": pct(0.5),
        "p95_seconds": pct(0.95),
        "min_seconds": durations[0],
        "max_seconds": durations[-1],
    }


def _base_image_id() -> str:
    return str(_docker().images.get(BASE_IMAGE).id)


# ---------------------------------------------------------------------------
# section 4: copy-in/out by size
# ---------------------------------------------------------------------------


_COPY_SIZES = {"1kib": 1024, "64kib": 64 * 1024, "1mib": 1024 * 1024, "8mib": 8 * 1024 * 1024}


def _copy_in_out(report: dict[str, Any]) -> None:
    adapter = DockerSandboxAdapter(_docker())
    image = _base_image_id()
    results: dict[str, Any] = {}
    for label, total in _COPY_SIZES.items():
        files = {"blob.bin": os.urandom(total)}
        spec = SandboxSpec(
            image=image,
            argv=("sleep", "30"),
            run_key=os.urandom(6).hex(),
            job_id="demo-transport",
        )
        kwargs = create_kwargs(
            spec,
            SandboxLimits(),
            f"engine-sandbox-demo-transport-{label}",
            {
                "engine-sandbox": "1",
                "engine-sandbox.created": str(int(time.time())),
                "engine-sandbox.job": "demo-transport",
                "engine-sandbox.attempt": "0",
            },
            "",
        )
        container = adapter.create(**kwargs)
        try:
            container.start()
            began = time.monotonic()
            copy_in(adapter, container, files)
            copy_in_seconds = round(time.monotonic() - began, 4)
            began = time.monotonic()
            out = copy_out(adapter, container, max_bytes=64 * 1024 * 1024)
            copy_out_seconds = round(time.monotonic() - began, 4)
            assert dict(out.files)["blob.bin"] == files["blob.bin"], "transport corrupted"
            results[label] = {
                "bytes": total,
                "copy_in_seconds": copy_in_seconds,
                "copy_out_seconds": copy_out_seconds,
                "roundtrip_ok": True,
            }
            print(
                f"OBSERVED copy-in/out {label}: in={copy_in_seconds}s "
                f"out={copy_out_seconds}s (verified)"
            )
        finally:
            container.remove(force=True)
    report["copy_in_out"] = results


# ---------------------------------------------------------------------------
# section 5: concurrency ramp (task 4)
# ---------------------------------------------------------------------------


def _concurrency_ramp(report: dict[str, Any]) -> None:
    adapter = DockerSandboxAdapter(_docker())
    image = _base_image_id()
    ramp: dict[str, Any] = {}
    max_ok = 0
    for n in (1, 2, 4, 6, 8, 10, 12):
        durations: list[float] = []
        failures: list[str] = []
        barrier = threading.Barrier(n)
        lock = threading.Lock()

        def one(
            index: int,
            barrier: threading.Barrier,
            lock: threading.Lock,
            durations: list[float],
            failures: list[str],
        ) -> None:
            try:
                barrier.wait(timeout=30)
                began = time.monotonic()
                run = run_sandbox(
                    SandboxSpec(
                        image=image,
                        argv=("python", "-c", "import time; time.sleep(2); print('ok')"),
                        env={"HOME": "/tmp"},
                        run_key=os.urandom(6).hex(),
                        job_id="demo-ramp",
                        attempt=index,
                    ),
                    adapter,
                    limits=SandboxLimits(mem_mb=256, wall_seconds=25),
                )
                elapsed = round(time.monotonic() - began, 3)
                with lock:
                    durations.append(elapsed)
                    if run.exit_code != 0 or run.stdout.strip() != b"ok":
                        failures.append(
                            f"exit={run.exit_code} stdout={run.stdout!r} oom={run.oom_killed}"
                        )
            except Exception as exc:  # noqa: BLE001 - a ramp failure IS the datum
                with lock:
                    failures.append(repr(exc)[:120])

        threads = [
            threading.Thread(
                target=one,
                args=(i, barrier, lock, durations, failures),
            )
            for i in range(n)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        durations.sort()
        p50 = median(durations) if durations else None
        ok = not failures and len(durations) == n
        ramp[str(n)] = {
            "ok": ok,
            "failures": failures[:3],
            "p50_seconds": p50,
            "max_seconds": durations[-1] if durations else None,
        }
        print(f"OBSERVED ramp n={n}: ok={ok} p50={p50} failures={len(failures)} {failures[:2]}")
        if ok:
            max_ok = n
        else:
            break
    print(f"OBSERVED max clean concurrency: {max_ok} sandboxes at 256 MiB each")
    report["concurrency_ramp"] = {
        "sandbox_mem_mb": 256,
        "max_clean_concurrency": max_ok,
        "ramp": ramp,
        "note": (
            "The per-worker default ENGINE_MAX_CONCURRENT_SANDBOXES is set "
            "below the host's clean maximum: the host runs several workers."
        ),
    }
    if max_ok < 4:
        raise SystemExit(f"concurrency ramp: only {max_ok} clean slots — tune the default")


def main() -> int:
    report: dict[str, Any] = {
        "date": datetime.now(UTC).strftime("%Y-%m-%d"),
        "environment": _machine_specs(),
        "docker_sdk": {
            "note": (
                "docker-py logs() demuxes client-side in 7.2.0; the runner "
                "reads the raw attach socket instead"
            )
        },
    }
    _install_cold_cached(report)
    _container_start(report)
    _copy_in_out(report)
    _concurrency_ramp(report)
    _kill_demo(report)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"sandbox-demo-{datetime.now(UTC).strftime('%Y%m%d')}.json"
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"report saved to {path}")
    leftovers = _count_leftovers(_docker())
    if leftovers:
        print(f"WARNING: {len(leftovers)} engine-sandbox containers left over")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
