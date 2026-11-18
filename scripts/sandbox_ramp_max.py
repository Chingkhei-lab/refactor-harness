"""Find the concurrency FAILURE POINT the demo ramp (n = 1…12) stops short of.

`scripts/sandbox_demo.py` ramps to 12 and reports "clean at every n", which is
a lower bound, not a maximum. This script keeps climbing the ladder until a
level fails, and records the level that failed — the only way to state a
measured host ceiling instead of ">= 12".

Usage (Docker daemon up, base image pulled):

    python scripts/sandbox_ramp_max.py --levels 64,80,88,96 --repeats 2 \
        --json docs/results/sandbox-ramp-$(date +%Y%m%d).json

Every level runs `n` sandboxes that start on a barrier, each running a 2 s
sleep payload under a 256 MiB limit (the demo's shape), so the numbers are
comparable to the ramp in docs/results/sandbox-2026-11-18.md. A level is CLEAN
when every sandbox exits 0 with the expected stdout; the first level with any
failure is reported and (unless --keep-going) the ladder stops there.

Evidence class matters: an `exit 137` with no completion marker means the
container was already gone, so its cgroup `oom_kill` counter cannot be read
(ADR 003 D7 rule 3) and the only available evidence is the daemon's
`State.OOMKilled` flag — which is reported as-is, never upgraded to a cause.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import docker  # type: ignore[import-untyped]

from engine.sandbox import DockerSandboxAdapter, SandboxLimits, SandboxSpec
from engine.sandbox import run as run_sandbox

PAYLOAD = "import time; time.sleep(2); print('ok')"
MEM_MB = 256
DEFAULT_WALL_SECONDS = 60.0


def _level(
    client: Any, adapter: DockerSandboxAdapter, image: str, n: int, wall_seconds: float
) -> dict[str, Any]:
    """Run `n` sandboxes at once; returns one level's observation."""
    durations: list[float] = []
    failures: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(n)

    def one(index: int) -> None:
        try:
            barrier.wait(timeout=180)
            began = time.monotonic()
            run = run_sandbox(
                SandboxSpec(
                    image=image,
                    argv=("python", "-c", PAYLOAD),
                    env={"HOME": "/tmp"},
                    run_key=os.urandom(6).hex(),
                    job_id="ramp-max",
                    attempt=index,
                ),
                adapter,
                limits=SandboxLimits(mem_mb=MEM_MB, wall_seconds=wall_seconds),
            )
            elapsed = round(time.monotonic() - began, 3)
            with lock:
                durations.append(elapsed)
                if run.exit_code != 0 or run.stdout.strip() != b"ok":
                    # The kills are the datum: record what the daemon said and
                    # nothing more (a 137 with no marker cannot be attributed).
                    failures.append(
                        f"exit={run.exit_code} oom_killed={run.oom_killed} "
                        f"timed_out={run.timed_out} stdout={run.stdout[:40]!r}"
                    )
        except Exception as exc:  # noqa: BLE001 - a ramp failure IS the observation
            with lock:
                failures.append(repr(exc)[:160])

    threads = [threading.Thread(target=one, args=(i,)) for i in range(n)]
    began = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=wall_seconds * 2)
    wall = round(time.monotonic() - began, 1)
    durations.sort()
    return {
        "n": n,
        "clean": not failures and len(durations) == n,
        "returned": len(durations),
        "p50_seconds": durations[len(durations) // 2] if durations else None,
        "max_seconds": durations[-1] if durations else None,
        "wall_seconds": wall,
        "failures": len(failures),
        "failure_samples": failures[:5],
    }


def _sweep(client: Any) -> int:
    """Remove every labeled container a failed level left behind."""
    removed = 0
    for container in client.containers.list(all=True, filters={"label": "engine-sandbox"}):
        container.remove(force=True)
        removed += 1
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--levels", default="64,80,88,96")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--image", default="python:3.12-slim")
    parser.add_argument("--wall-seconds", type=float, default=DEFAULT_WALL_SECONDS)
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="do not stop at the first failing level",
    )
    parser.add_argument("--json", default="", help="write the observations to this path")
    args = parser.parse_args()

    client = docker.from_env()
    info = client.info()
    daemon = {
        "server_version": info.get("ServerVersion"),
        "vm_mem_gib": round((info.get("MemTotal") or 0) / 1024**3, 2),
        "vm_cpus": info.get("NCPU"),
        "image": args.image,
        "mem_limit_mb": MEM_MB,
        "payload": PAYLOAD,
        "wall_seconds": args.wall_seconds,
    }
    server, gib, cpus = daemon["server_version"], daemon["vm_mem_gib"], daemon["vm_cpus"]
    print(f"daemon {server} mem={gib}GiB ncpu={cpus}")

    image = client.images.get(args.image).id
    adapter = DockerSandboxAdapter(client)
    observations: list[dict[str, Any]] = []
    max_clean = 0
    failure_level: int | None = None

    for n in (int(part) for part in args.levels.split(",") if part.strip()):
        for repeat in range(args.repeats):
            observed = _level(client, adapter, image, n, args.wall_seconds)
            observed["repeat"] = repeat + 1
            observations.append(observed)
            print(
                f"OBSERVED ramp-max n={n} repeat={repeat + 1}: clean={observed['clean']} "
                f"p50={observed['p50_seconds']} wall={observed['wall_seconds']}s "
                f"failures={observed['failures']} {observed['failure_samples']}"
            )
            if observed["clean"]:
                max_clean = max(max_clean, n)
            elif failure_level is None:
                failure_level = n
            time.sleep(2.0)  # let the daemon settle between runs
        if failure_level is not None and not args.keep_going:
            break

    cleaned = _sweep(client)
    summary = {
        "daemon": daemon,
        "levels": observations,
        "max_clean": max_clean,
        "first_failure": failure_level,
        "containers_swept": cleaned,
    }
    print(
        f"OBSERVED ramp-max result: max_clean={max_clean} "
        f"first_failure={failure_level} swept={cleaned}"
    )
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2, sort_keys=True)
            handle.write("\n")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
