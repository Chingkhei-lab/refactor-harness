# refactor-harness

A Postgres-backed background job engine: durable `jobs` table, `SKIP LOCKED`
claiming, fencing via `(locked_by, attempts)`, long-lived heartbeats, a reaper
for expired leases, and at-least-once delivery with bounded retries.

- Design: [`docs/design.md`](docs/design.md) · ADRs: [`docs/adr/`](docs/adr/)
- Queue SQL (the review artifacts): [`src/engine/sql/`](src/engine/sql/)
- CI: ruff + mypy + pytest (Python 3.12, Postgres 16 service container)

## Results

Chaos v1 (2026-11-15) — full report:
[`docs/results/chaos-20261115.json`](docs/results/chaos-20261115.json)

| Metric | Value |
| --- | --- |
| Jobs | 10,000 |
| Workers / hard kills / restarts | 3 / 26 / 26 |
| Lost jobs | 0 (10,000 succeeded, 0 dead) |
| Violations (same `(job_id, attempt)` executed twice) | 0 |
| Attempts histogram | `{1: 9979, 2: 21}` |
| Duplicate side effects (at-least-once baseline) | 19 |
| Duration / throughput | 137.1 s / 72.9 jobs/s |
| Config | 4 s lease, 1 s reaper interval, 5–30 ms handler sleep, max 5 attempts |

Environment: Docker (Linux, WSL2) on a 4-CPU / 3.8 GB Windows machine,
PostgreSQL 16.15.

> **Throughput caveat:** 72.9 jobs/s is an end-to-end pipeline number for
> *this* environment — Docker bridge to Postgres, handler sleeps, and 26
> kill/restart cycles included. It measures the pipeline, not Postgres; it is
> not a database benchmark.

Chaos v2 (2026-11-16) — durable workflows under the same kill loop; full
report: [`docs/results/chaos-v2-20261116.json`](docs/results/chaos-v2-20261116.json)

| Metric | Value |
| --- | --- |
| Workflows | 2,000 × 4 steps (one 2-second delay step each) |
| Injected failures | 2% always-failing (40) + 1% NonRetryable (20) |
| Workers / hard kills / restarts | 3 / 59 / 59 (83.1% of kills hit a running job) |
| Terminal | 1,940 succeeded + 60 failed == exactly the injected count |
| Duplicate executions / duplicate effects | 122 (> 0, expected) / **0** |
| Claim violations (same `(job_id, attempt)` executed twice) | 0 |
| Succeeded workflows: one effect row per step, sums `1→3→6→10` | 1,940 / 1,940 |
| Every failed workflow in `dead_letters` | 60 / 60 |
| Reconciler latency (job death → workflow failed) | p50 0.27 s, p95 0.64 s, max 0.89 s |
| Replay of one failed workflow (pardon + DLQ) | succeeded in 4.0 s, full effect chain |
| Duration / throughput | 297.0 s / 26.5 jobs/s |
| Config | 4 s lease, 1 s reaper + reconciler interval, 0.2 s backoff base, 8-attempt budget on sum steps |

Environment: same as v1 (Docker Linux on WSL2, 4 CPUs / 3.8 GB, PostgreSQL
16.15); workflow step handlers sleep 20–60 ms, so jobs/s is not comparable to
v1's plain handlers — see the caveat above.

Phase 2 (durable workflows) targets the 19 duplicate effects: step outputs
become first-class, and effects are deduplicated by `effect_key`, driving the
at-least-once baseline to zero visible duplicates. Chaos v2 is that proof:
duplicate executions happen under kills (122), duplicate effects never do.
