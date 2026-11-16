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
| Redelivered executions absorbed by idempotency keys | 122 |
| Duplicate effects | 0 — true by construction (`effect_key UNIQUE` + `ON CONFLICT DO NOTHING`), so the evidence is the 122 absorbed redeliveries above plus the per-step effect/sum check below |
| Claim violations (same `(job_id, attempt)` executed twice) | 0 |
| Succeeded workflows: one effect row per step, sums `1→3→6→10` | 1,940 / 1,940 |
| Every failed workflow in `dead_letters` | 60 / 60 |
| Reconciler latency (job death → workflow failed; bounded by the 1 s reconciler interval) | p50 0.27 s, p95 0.64 s, max 0.89 s |
| Replay of one failed workflow (pardon + DLQ) | succeeded in 4.0 s, full effect chain |
| Duration / chaos-run throughput | 297.0 s / 26.5 jobs/s |
| Config | 4 s lease (1.33 s heartbeat), 1 s reaper + reconciler interval, 0.2 s backoff base, 8-attempt budget on sum steps |

> **Effect idempotency is a handler contract, not an engine guarantee.** The
> engine's part is a stable `effect_key` (workflow id + step name) in the step
> context; the dedup itself — insert with `ON CONFLICT DO NOTHING` on that key
> — lives in the handler. "0 duplicate effects" is therefore true by
> construction, not a measurement.

> **The replay phase used a test hook.** A `pardon` scaffolding row told the
> always-failing step to succeed, then `dlq replay` requeued its dead job —
> that is how the replay was observable at all. In real use, replay only
> helps once the underlying cause is fixed: replaying an unchanged poison
> step just deads it again.

Environment: same as v1 (Docker Linux on WSL2, 4 CPUs / 3.8 GB, PostgreSQL
16.15); workflow step handlers sleep 20–60 ms and each workflow carries a
2-second timer step, so 26.5 jobs/s is a chaos-run figure for THIS run, not a
capacity number and not comparable to v1's plain handlers — see the caveat
above.

Phase 2 (durable workflows) targets the 19 duplicate effects: step outputs
become first-class, and effects are deduplicated by `effect_key`. Chaos v2 is
that proof: under 59 hard kills, 122 redelivered executions were absorbed by
idempotency keys and 0 duplicate effects appeared.

## Sandbox (Phase 3, checkpoint 7)

Untrusted code runs in Docker containers: read-only rootfs, non-root,
no network, capped CPU/memory/pids/disk/output, tmpfs workspace transported
through the exec'd tar boundary (ADR
[`docs/adr/003-sandbox.md`](docs/adr/003-sandbox.md)). 18-case hostile
suite runs in CI (`sandbox-hostile` job) against a real daemon.

> **The sandbox protects the host, not the verdict.** Everything the
> container reports — exit codes, output, the completion marker — is
> untrusted input; only the host-side evidence (cgroup counters, caps,
> deadlines) decides what actually happened, and anything that turns a run
> into a pass/fail verdict must re-verify it outside the container.
