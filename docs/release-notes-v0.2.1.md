# v0.2.1 — sandbox-test-run workflow + install phase + mini-chaos

Postgres-backed job engine with durable workflows. Checkpoint 9 deliverables:
the `sandbox-test-run` workflow, the cached install phase, the typed tool layer,
and the mini-chaos evidence underneath all of it.

## Measurements

Windows 10.0.26300, Docker 28.3.2 (Linux VM: 4 CPUs / 3.83 GiB), docker-py
7.2.0, PostgreSQL 16.

| Measurement | Result |
| --- | --- |
| Install cold / cached | 10.031 s / 0.234 s (same image ID; key = sha256(requirements ∥ base digest)) |
| Container start (n=30) | p50 0.375 s, p95 0.532 s |
| Copy-in/out (1 KiB…8 MiB) | 0.14–0.6 s each way, roundtrip verified |
| Concurrency ramp (256 MiB) | clean at n=96 (3 runs) and n=128 (1 run) with a 60 s per-sandbox deadline; host wall (3.83 GiB VM) not reached. The one earlier 96 failure was the demo's 25 s wall clock, not memory |
| Kill demo | orphan swept in 0.828 s; workflow succeeded 41.6 s after the kill; one `sandbox_runs` row; zero leftovers |
| Mini-chaos (200 workflows, 3 workers, kill every 3–6 s) | **200/200 succeeded** in 2786.2 s; 553 kills (543 mid-job); zero duplicate `(job_id, attempt)` rows, zero orphans, zero leftovers; regression tests green; deepest victim needed 24 attempts |

## Mini-chaos progression (all four runs preserved)

- Run 1 (lease 4 s): never drained — queue math, not correctness.
- Run 2 (lease 2 s, step budget 5): drained in 2210.7 s, but 42/200
  dead-lettered — mid-job kills exhausted the 5-attempt budget. A murdered
  job costs an attempt like any other failure.
- Run 3 (step budget 40, `ENGINE_SANDBOX_STEP_BUDGET`): all 200 succeeded; a
  single step absorbed 23 consecutive murders, because as the pool drains
  every kill lands on the few jobs still in flight.

**The 24-attempt step is a budget-sizing datum from a bombardment, not an
operating point.** 553 kills over 200 workflows says the budget is sized
correctly; it does not say a normal run needs 24 tries.

Across all four runs the kill loop injected **1,702 kills, 1,677 of them hitting
a running job** (233/232, 440/428, 476/474, 553/543). That total is a
bombardment statistic — it only means something attached to its setup: 200
`sandbox-test-run` workflows, 3 workers, a hard kill every 3–6 s, and a step
budget raised 5 → 40 after run 2.

## Concurrency: corrected finding

The ramp was measured past the demo's n = 12, and the result corrects an
earlier claim. n = 96 failed once (6 of 96 containers exited 137) and the
failure was attributed to the Docker VM's memory ceiling. Re-measured with the
per-sandbox deadline raised from 25 s to 60 s, **n = 96 was clean three times
and n = 128 clean once**; p50 latency climbs 2.9 s → 59 s across the ladder.

So the host wall was never reached in this range — the binding constraint was
the 25 s deadline, whose p50 (24.4 s) was already at the limit. Latency, not
container count, is what scales. Residual unknown: a 137 with no completion
marker leaves no readable cgroup `oom_kill` counter (ADR 003 D7 rule 3), so
"deadline" is the supported explanation rather than a probe-level proof. The
per-worker default of 4 × 3 = 12 concurrent is ~9 % of the highest
measured-clean level.

## CI

- run #20 (`c0a9c3d`): `ci` 249 passed / 25 skipped / 0 failed;
  `sandbox-hostile` 18 passed / 0 failed.
- The 6-case install-hostile suite previously ran only locally; the follow-up
  commit adds it to the `sandbox-hostile` job (real pip + Docker, wheels only).
- Tag `v0.2.1` is the checkpoint-9 commit; these review-round-1 corrections
  land on `main` after it.

## Artifacts

`docs/results/sandbox-2026-11-18.md` and the JSON files beside it, including
`sandbox-ramp-20261118.json` for the concurrency ladder.
