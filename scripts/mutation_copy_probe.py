"""Time just the mutant-write step for the 100 sampled sites of the published run.

The 100-mutant report records a per-mutant total and the suite's own duration, so
`total - suite` is everything else. This measures the copy half of "everything
else", on the same sites and the same tree, so the breakdown reconciles exactly:

    total = copy + pass + suite
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "src"))

import mutate as runner  # noqa: E402

from harness.mutate import enumerate_sites  # noqa: E402

REPORT = REPO / "docs/results/mutation-pycodestyle-20261121.json"
TARGET = REPO / ".harness/verify-trees/old"
MODULE = "pycodestyle"


def main() -> int:
    payload = json.loads(REPORT.read_text(encoding="utf-8"))
    sample = [row["site"] for row in payload["results"]]
    source = runner._module_file(TARGET, MODULE).read_text(encoding="utf-8")
    sites = {site.key: site for site in enumerate_sites(source)}
    work = REPO / ".harness/mutation-timing/copy-probe"
    timings: dict[str, float] = {}
    for key in sample:
        started = time.monotonic()
        runner._write_mutant(TARGET, work, sites[key], source, MODULE)
        timings[key] = time.monotonic() - started
    values = list(timings.values())
    out = {
        "sites": len(values),
        "copy_total_seconds": round(sum(values), 2),
        "copy_mean_seconds": round(statistics.mean(values), 3),
        "copy_median_seconds": round(statistics.median(values), 3),
        "copy_max_seconds": round(max(values), 3),
        "per_site": {key: round(value, 3) for key, value in timings.items()},
    }
    path = REPO / ".harness/mutation-timing/copy-probe.json"
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k != "per_site"}, indent=2))
    print(f"written {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
