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

Phase 2 (durable workflows) targets the 19 duplicate effects: step outputs
become first-class, and effects are deduplicated by `effect_key`, driving the
at-least-once baseline to zero visible duplicates.
