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

[v0.3](https://github.com/Chingkhei-lab/refactor-harness/releases/tag/v0.3) —
checkpoint 12: CLI observation layer, approvals, the optparse→argparse migration
and the final cross-target report
([`docs/results/harness-20261120.md`](docs/results/harness-20261120.md)).

[v0.2.2](https://github.com/Chingkhei-lab/refactor-harness/releases/tag/v0.2.2) —
checkpoint 9 review rounds 1–2 (corrected concurrency finding, demo/test
database split, self-reporting CI, install network probe). Supersedes
[v0.2.1](https://github.com/Chingkhei-lab/refactor-harness/releases/tag/v0.2.1).

## Model routing (Phase 5)

The agent loop needs a model, and the listing numbers do not measure what matters
for a loop: whether the tool calls arrive as well-formed JSON. So three finalists
were chosen by one trait each — availability, generation speed, and raw capability
— and measured on the same 20 tasks against the same tools.

| step | result |
| --- | --- |
| Smoke test: one `read_file(path)` call, then a tool-result turn | **3/3 pass**; every model returned `read_file({"path":"calc/core.py"})` as a structured call |
| Stage 1: 12 tasks × 2 runs | nemotron **91.7 %**, kimi 83.3 %, deepseek 70.8 % |
| Stage 2: 20 tasks × 3 runs, top two | **nemotron 93.3 %** (95 % CI 84.1–97.4), **kimi 80.0 %** (68.2–88.2) |
| Valid tool calls | **100 %** for all three, 639 calls, no malformed arguments |
| Decision | **primary `nvidia/nemotron-3-ultra-550b-a55b:free`**, fallback `moonshotai/kimi-k3.1-pro:free` |
| Provider fallback | `gemini-3.8-flash` passes both smoke turns; its free tier took **460 s** for one call |

What separated them was not correctness but finishing: kimi used 341 tool calls
where nemotron used 298 for the same 60 runs, and spent its budget on exploration
(`multi-two-files`, `multi-rename`, one injection task: 3/3 failures each,
"call budget exceeded"). Both fail the deliberately-tight 4-call budget task, so
neither is disciplined enough to be given a hard cap without slack.

Report: [`docs/results/model-bakeoff-20261122.md`](docs/results/model-bakeoff-20261122.md).
Tasks, tools and scoring live in `scripts/bakeoff_tasks.py`,
`scripts/bakeoff_tools.py` and `scripts/model_bakeoff.py`; `--self-check` proves
every task fails before its reference fix and passes after, and `--analyse`
rebuilds the report from cassettes without a network call.

### Data policy for these calls

**Assume every prompt and every response is logged and may be read by a human
reviewer.**

- Only open-source target code is sent: the `calc` fixture here, and
  `parse`/`pycodestyle` sources that are already public. No secrets, keys, customer
  data or private repository content goes into a prompt.
- Keys live in `.harness/bakeoff/keys.env`, which is git-ignored; no cassette
  records an `Authorization` header (`redact()`, plus a unit test).
- Gemini's free tier, quoted from <https://ai.google.dev/gemini-api/terms>: for
  Unpaid Services — "the unpaid quota on Gemini API" — Google "uses the content you
  submit to the Services and any generated responses to provide, improve, and
  develop Google products and services and machine learning technologies", and
  "human reviewers may read, annotate, and process your API input and output". The
  page's own instruction is "Do not submit sensitive, confidential, or personal
  information." The same assumption is made for numes.ai, whose policy was not
  available to check.

## Architecture

Two things live in this repository: a **job engine** (`src/engine`, Postgres,
Phase 1–3) and a **behaviour harness** (`src/harness`, Phase 4). The harness is the
part that answers "did this refactor change behaviour", and it is built as a
pipeline where every stage can refuse to produce a number.

```
 target tree (old) ─┐
                    ├─► run_pass ─► observations ─┐
 target tree (new) ─┘        │                    │
                             │                    ├─► compare_sets ─► verdicts
   cases + shim ─────────────┘                    │        │
   (determinism controls,                         │        ├─ rules  (mask a field,
    fixed environment)                            │        │         with a reason)
                                                  │        └─ approvals (accept a diff,
   sandbox: copy-in tar → tmpfs →                 │                   bound to its sha256)
   container → copy-out tar                       │
                                                  └─► report: counts, unsupported,
                                                      coverage, A/A noise, hashes
```

| stage | module | the rule it enforces |
| --- | --- | --- |
| cases | `harness/cases.py`, `harness/cases/*.json` | a case is data; the set has a sha256 and every report carries it |
| determinism | `harness/determinism.py` | clock, hash seed, uuid, random, cwd, locale, timezone are controlled before the target is imported |
| entry kinds | `harness/child.py` | `call` observes a function's value; `cli` observes a program's exit, streams and written files; `api` observes a module's public surface (and what an entry function returns) |
| execution | `harness/runner.py`, `engine/sandbox.py` | sandboxed by default: no network, read-only rootfs, dropped caps, cgroup limits; workspace as tar in/out, never a host mount |
| observation | `harness/observation.py`, `harness/projections.py` | one canonical encoding, schema-versioned, opaque values are `unsupported` and counted; a declared projection (a traceback as type, message and frames) is named in the observation |
| evaluation | `harness/mutate.py`, `scripts/mutation_audit.py` | a mutant is one contiguous edit on the site's line, checked; a kill is only counted after asking whether the case could have made it |
| comparison | `harness/compare.py` | `identical` / `equivalent` / `equivalent_bounded` / `different` / `undecidable`; nothing is "maybe equal" |
| normalisation | `harness/rules.py`, `harness/rules/*.json` | a rule is data, schema-validated, justified, hash-pinned; a refused rule never stops the walk |
| approvals | `harness/approvals.py`, `harness/approvals/*.json` | an accepted difference is bound to the observed diff; a changed diff makes the approval stale |
| report | `scripts/verify_change.py` | a report without counts, unsupported cases, coverage, A/A noise and both hashes is not a report |

The **second oracle** is the target's own test suite, run untouched. It is checked
before its numbers are quoted (`scripts/oracle_canary.py`: the clean tree passes, a
known-broken mutant fails) and pinned so the suite and the module come from the
same release (`scripts/pin_oracle.py`).

The **evaluator** is checked the same way, because a kill rate is only as good as
the mutants: `scripts/mutation_canary.py` runs four behaviour-preserving mutants
through the whole pipeline and requires the harness to be silent on three of them
(a comment, a whitespace change, a full `ast.unparse` re-print) and to see the
fourth (a docstring word) in exactly the two api cases. It is a CI step, so an
evaluator that starts detecting "the file changed" fails the build.

## Behavior harness (Phase 4)

Design: [`docs/design-harness.md`](docs/design-harness.md) · ADR:
[`docs/adr/004-harness.md`](docs/adr/004-harness.md) · first baseline:
[`docs/results/harness-parse-20261119.md`](docs/results/harness-parse-20261119.md) ·
**final report: [`docs/results/harness-20261120.md`](docs/results/harness-20261120.md)**.

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

### Stage 2: a CLI migration, and what each oracle sees

Stage 2 changes a command line, so its cases run a program. `pycodestyle` 2.15.0
(`optparse`) migrates to `argparse`; the change under test is
[`harness/migrations/pycodestyle-argparse.patch`](harness/migrations/pycodestyle-argparse.patch).

| Measurement | Value |
| --- | --- |
| Case set | **79** (42 fixture excerpts + 35 option probes + 2 api snapshots), hash `8f04551c…` |
| A/A over that set | **0 differences, 5 fixed + 5 shuffled passes** |
| Coverage incl. subprocess | lines **89.74 %**, branches **82.62 %**, combined **87.11 %**; `get_parser` 23/23 |
| verify-change, 2 rounds × 79 | **142 identical, 16 different (8 approved), 0 unapproved**, 0 unsupported, 0 flaky |
| Upstream suite on the migrated tree | 4 failures: 3 usage-text assertions, 1 `add_option` API break |
| Mutation, 100 mutants seed 21, both oracles | harness **62/100 = 62 %**, suite **74/100 = 74 %**, **4 kills the suite misses**, 22 the pair misses |
| Unintended change the cases caught | `--version` printed `pycodestyle 2.15.0` — fixed, **not approved**; pre-fix report kept |

Two numbers here were wrong first and are worth knowing about. The mutation run
that reported **100/100** was measuring a mutator that re-printed the whole module
on every mutant, so two cases that print a traceback differed for all 100 of them;
the audit that found it — and the corrected 62 % — is
[`docs/results/checkpoint12b-audit-20261121.md`](docs/results/checkpoint12b-audit-20261121.md).
And `get_parser().add_option` used to be **suite only**: a program cannot show an
API nobody calls, so the `api` case kind snapshots the module's public surface and
the object `get_parser()` returns. That row is now seen by both oracles, as an
approved difference with a witness. A snapshot still cannot *exercise* a contract,
which is why two `register_check` behaviours remain the suite's alone.

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
