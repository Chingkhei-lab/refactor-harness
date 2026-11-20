# refactor-harness

A Postgres-backed background job engine: durable `jobs` table, `SKIP LOCKED`
claiming, fencing via `(locked_by, attempts)`, long-lived heartbeats, a reaper
for expired leases, and at-least-once delivery with bounded retries.

- Design: [`docs/design.md`](docs/design.md) · ADRs: [`docs/adr/`](docs/adr/)
- Queue SQL (the review artifacts): [`src/engine/sql/`](src/engine/sql/)
- CI: ruff + mypy + pytest (Python 3.12, Postgres 16 service container; run #20: 249 passed / 25 skipped)

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

## Releases

[v0.2.2](https://github.com/Chingkhei-lab/refactor-harness/releases/tag/v0.2.2) —
checkpoint 9 review rounds 1–2 (corrected concurrency finding, demo/test
database split, self-reporting CI, install network probe). Supersedes
[v0.2.1](https://github.com/Chingkhei-lab/refactor-harness/releases/tag/v0.2.1).

## Behavior harness (Phase 4)

Design: [`docs/design-harness.md`](docs/design-harness.md) · ADR:
[`docs/adr/004-harness.md`](docs/adr/004-harness.md) · first baseline:
[`docs/results/harness-parse-20261119.md`](docs/results/harness-parse-20261119.md).

The harness gives **evidence, not a proof of equivalence**. Stage 1 runs
against `parse` 1.22.4 — a **calibration target for the harness, not a legacy
codebase**: 1 106 lines, zero dependencies, wheels only, deliberately boring so
that a misbehaving harness cannot hide behind an interesting target. The
legacy-modernisation story starts at stage 2/3, chosen after the harness works.

Measurement, so far: 273 cases, 0 A/A noise over 5 fixed and 5 shuffled passes,
0 differences between the target and an identical copy and between the target and
12 harmless refactors; one hand-seeded bug inside such a refactor was caught
(4 cases). After the `evaluate_result` projection: 261 return / 12 exception /
**0 unsupported** (was 23), line coverage 426/554. Observation schema v2; the
checkpoint-10 numbers reproduce exactly under it (238 / 12 / 23, 273 blobs).

Does the harness actually catch wrong behaviour? Two evaluations answer that, and
neither has a target number to hit:

| Question | Answer |
| --- | --- |
| Mutants killed, 300 sampled of 839 sites (seed 12) | **139 / 300 = 46.3 %** (95 % CI 41-52) |
| … of the 203 mutants on a line the cases execute | **138 / 203 = 68.0 %** (95 % CI 61-74) |
| Upstream test suite alone | 259 / 300 = 86.3 % |
| … and the overlap between the two oracles | **139 both, 0 harness-only, 120 tests-only, 41 neither** |
| Survivors | 96 on a line never executed, 65 covered-but-not-observed, 0 equivalent |
| 12 harmless refactors (6 mechanical + 6 hand-written) | all clean, **0 false positives** |
| One hand-seeded bug inside a harmless refactor | **different on 4 cases** |
| Tuning the case set for the survivor clusters | 46.3 % → 60.0 % on the same mutants (overfit), **38.7 % → 55.3 % held-out** |

The contrarian result is the useful one, and it is the first thing to say about
stage 1: **on `parse` every harness kill is also an upstream-test kill**, so the
case set adds no kill power to a library that already has 109 tests. The 120
tests-only kills mark exactly where it is blind. Stage 1's job was to prove the
harness sees a change at all — and that it can say "no change" without crying
wolf — not to beat a mature suite.

**The claim that matters is about the other kind of target.** Legacy code usually
has **no suite**, so the honest statement of what this harness does is: *generated
cases reach about half of the upstream suite's kill power with zero hand-written
assertions, and on mutants the tuning never saw they reach 73 % of it.*

| Evidence | Number |
| --- | --- |
| First pass, 300 mutants (seed 12) | 139/300 = 46.3 % (95 % CI 41-52) |
| Upstream suite on the same 300 | 259/300 = 86.3 % — harness kills are a subset |
| Tuned on those mutants (overfit by construction) | 180/300 = 60.0 % |
| Held-out, 150 mutants from the 539 untouched sites | 58/150 = 38.7 % → **83/150 = 55.3 %** tuned |
| Held-out with the suite as second oracle | harness 83, suite 113, **4 harness-only**, 79 both |
| 12 harmless refactors | all clean, **0 false positives** |
| One hand-seeded bug inside a harmless refactor | **different on 4 cases** |
| 41 mutants both oracles missed, reviewed by hand | 7 docstring-only, 33 real gaps, 1 candidate equivalent |

Two caveats are part of the claim, not footnotes: the held-out sites sit in the
same functions as the tuned clusters (so it shows regional coverage, not
cross-target generalisation), and 32 of the 41 both-missed mutants sit on a line no
case executes. Both are in `docs/results/mutation-tuning-20261120.md` and
`docs/results/mutation-both-missed-20261120.md`.

### Stage 2: a CLI migration, and the parser swap it approves

Stage 2 changes a command line, so its cases run a program. `pycodestyle` 2.15.0
(`optparse`) migrates to `argparse`; the change under test is
[`harness/migrations/pycodestyle-argparse.patch`](harness/migrations/pycodestyle-argparse.patch).

| Measurement | Value |
| --- | --- |
| CLI case set | **61 cases** (42 fixture excerpts + 19 option probes), hash `d2f78454511f64aa` |
| A/A over that set (2 fixed + 2 shuffled passes) | **0 differences** |
| verify-change, 2 rounds × 61 cases | 112 identical, **10 different (approved)**, **0 unapproved**, 0 unsupported, 0 flaky |
| Upstream suite on the migrated tree | 4 failures: 3 usage-text assertions, 1 test calling the optparse-only `add_option` |
| Unintended change found by the cases | `--version` printed `pycodestyle 2.15.0` instead of `2.15.0` — fixed, then re-verified |

Approved differences are parser **text** (help layout, `invalid int value`,
`unrecognized arguments`), each bound to the observed diff by sha256 and readable
in `harness/approvals/pycodestyle.json`; a changed diff makes the approval stale
and the case `different` again. Nothing approved is counted as `equivalent`.

Stage-2 mutation, subprocess coverage and the final cross-target report are
**not done** — see [`docs/results/stage2-cli-20261120.md`](docs/results/stage2-cli-20261120.md).
`v0.3` is deliberately untagged: tagging now would put a release number on work
whose own report says which numbers are missing.

### Known target bugs (not harness bugs)

- **`parse.findall` never terminates when the format can match the empty
  string.** `list(parse.findall("{:tt}", "22:13:20"))` yields the zero-width
  match at offset 8 forever. Reproduced on 1.22.4 with no shim, no sandbox and an
  external timeout (exit 124). Upstream: open pull request
  [r1chardj0n3s/parse#246](https://github.com/r1chardj0n3s/parse/pull/246)
  ("Avoid infinite findall iteration for empty matches", 2026-09-12, unmerged in
  1.22.4). The harness records this case as a truncated observation (1 000 items)
  rather than hanging, and never as "equal".

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

### Sandbox measurements (checkpoint 9, 2026-11-18)

Full report with every OBSERVED number:
[`docs/results/sandbox-2026-11-18.md`](docs/results/sandbox-2026-11-18.md).

| Measurement | Result |
| --- | --- |
| Install cold / cached | 10.031 s / 0.234 s (same image ID; key = sha256(requirements ∥ base digest)) |
| Container start (n=30) | p50 0.375 s, p95 0.532 s |
| Copy-in/out roundtrip (1 KiB…8 MiB) | 0.14–0.6 s each way, integrity verified |
| Concurrency ramp, 256 MiB sandboxes | **no memory wall observed up to n=128 with sleep payloads** (60 s per-sandbox deadline, `scripts/sandbox_ramp_max.py`; p50 24–59 s). Real capacity will be measured later with a memory-touching payload. The one 96 failure (6/96 exit 137) was the demo's **25 s** deadline, not host memory. Default 4 × 3 = 12 stays as headroom |
| Kill demo | orphan swept in 0.828 s; workflow succeeded 41.6 s after the kill; one `sandbox_runs` row (the committing attempt); zero leftovers |
| Mini-chaos (200 workflows, 3 workers, kill every 3–6 s) | drained in 2786.2 s: **200/200 succeeded, 0 failed**; zero duplicate `(job_id, attempt)` rows, zero orphan rows, one row per succeeded workflow, zero leftover containers; regression tests green; deepest victim needed 24 attempts (budget 40 absorbed it) |

The mini-chaos ran three times, and the report preserves why: run 1 (lease
4 s) never drained — queue math, not correctness; run 2 drained but
dead-lettered 42 workflows whose 5-attempt step budget was exhausted by 428
mid-job kills (a murdered job costs an attempt — engine behaviour correct,
budget wrong for a bombardment); run 3 sizes the retry budget via
`ENGINE_SANDBOX_STEP_BUDGET`.

The 24-attempt step is a **budget-sizing datum from a bombardment, not an
operating point**: 553 kills over 200 workflows, and as the pool drains every
remaining kill lands on the few jobs still in flight. A murdered job costs an
attempt like any other failure, so the number says the budget is sized
correctly — it does not say a normal run needs 24 tries.
