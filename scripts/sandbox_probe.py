"""One sandboxed pass, with every failure mode printed as a single line.

Diagnostics only: when the transport fails in CI there are no job logs to read, so
this prints what the container actually said (exit code, stderr tail, copied-out
file names) instead of a traceback.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from harness import determinism  # noqa: E402
from harness.cases import Case, case_set_hash  # noqa: E402
from harness.runner import run_pass  # noqa: E402
from harness.sandbox_transport import DockerExecutor  # noqa: E402


def main() -> int:
    import docker

    checkout = REPO / "tests" / "fixtures" / "canary"
    cases = [Case(id="0001", source="probe", entry="absolute", args=(-3,))]
    try:
        result = run_pass(
            python=sys.executable,
            checkout=checkout,
            shim_dir=determinism.write_shim(REPO / ".harness" / "probe-shim"),
            src_dir=REPO / "src",
            target_paths=[checkout],
            cases=cases,
            index=1,
            module="canary",
            workdir=REPO / ".harness" / "probe-work",
            expected_case_set_hash=case_set_hash(cases),
            executor=DockerExecutor(docker.from_env(), "python:3.12-slim"),
        )
        obs = [o.to_json() for o in result.observations]
        print("PROBE ok: " + json.dumps(obs)[:400])
        return 0
    except Exception as exc:  # noqa: BLE001 - the point is to report it
        detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        print(f"PROBE failed: {detail}"[:900])
        files = sorted(p.name for p in (REPO / ".harness" / "probe-work").glob("*"))
        print("PROBE workdir files: " + json.dumps(files)[:400])
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
