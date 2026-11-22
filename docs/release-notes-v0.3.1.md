# v0.3.1 — corrected stage-2 results

**Supersedes `v0.3`.** The tag `v0.3` stays where it is (`603f90a`); this release
carries the corrected numbers, and the `v0.3` release notes now say so at the top.

If you read one thing: **the stage-2 mutation result in `v0.3` was wrong — 100 of
100 was the mutator, not the harness. The corrected figure is 62 of 100 for the
harness and 74 of 100 for the upstream suite.**

## What was wrong

`v0.3` reported that both oracles killed every one of 100 mutants of
`pycodestyle.py`. The mutator serialised the whole module with `ast.unparse()`
after changing one node, so each "mutant" rewrote 2 717 lines into 2 095. Two CLI
cases print a traceback and a warning naming the target's own file and line, so 77
and 75 of 100 mutants differed on those two cases alone, and 38 differed on exactly
that pair. Fifteen more "kills" were on lines no case executes.

Two adjacent defects came out of the same audit:

- **The CLI observation contained the target's source.** A traceback names the
  file, line and statement, so any staged tree was a difference. The observation
  now projects a traceback to exception type, message and frame names.
- **An API nobody calls was invisible.** `get_parser()` no longer offering
  `add_option` was seen by the suite and by no case. A new `api` case kind
  snapshots the module surface and the returned object.

## Corrected numbers

| | both | harness only | suite only | neither |
| --- | --- | --- | --- | --- |
| stage 2, 100 mutants, seed 21 | **58** | **4** | **16** | **22** |

Harness **62/100 = 62.0 %** (95 % CI 52.2–70.9); upstream suite **74/100 = 74.0 %**
(64.6–81.6). No kill lands on an unexecuted line. Stage 1 is unchanged: all 300
`parse` verdicts reproduced identically after the mutator fix, which is how the
defect was localised to stage 2.

## Timing, from one run

Copy `32.5 s` + harness pass `4 244.7 s` + suite `1 387.7 s` = **5 664.8 s** for 100
mutants — **56.65 s per mutant**, 94.4 minutes wall clock. The earlier `32.4 s`
figure mixed a `-x` suite run (9.3 s) with the pass time of a different run; the
published run needs failing-test counts, so the suite costs 13.88 s per mutant, not
9.3 s. The parts sum to the recorded totals to 0.1 s.

## Why the other 38 survived

Classified with statement-span-aware coverage: `suite only` 16 = 5 on unexecuted
lines + 11 covered but not observed; `neither` 22 = 10 unexecuted + 12 covered but
not observed. All twelve covered survivors were read by hand and **none is an
equivalent mutant**: they are input shapes the case set never produced (E301/E306,
E721, E12x, BOM, a second diff hunk) or output modes it never examines
(`--benchmark`, which is declared unstable and unordered). Six of the twelve are
now closed by the tuning round; the table is in
[`docs/results/harness-20261122.md`](harness-20261122.md) §4.

## The CI copy phase was a fixed 10 s

The gate's `copy_in_and_copy_out` measured 10.146 s on a GitHub runner for a 238 KB
workspace, against 0.14 s on the author's host. It was not throughput: `stop()`
closed the attach socket and then joined the reader, and on Linux `close()` does
not wake a `recv()` blocked in another thread, so every pass with a workspace paid
the full 10 s join timeout. Now `shutdown(SHUT_RDWR)` precedes `close()`, and
`tests/test_sandbox.py::test_stopping_the_log_reader_wakes_a_blocked_recv` models
the Linux behaviour and fails on the old code.

## Two harness defects found by the tuning round

Both made the harness blame the target for a harness failure:

- **A locked case directory turned a case into `unsupported`, permanently.** The
  directory is fixed by case id on purpose (`-vv` prints it), and
  `rmtree(ignore_errors=True)` hid a directory held by another process, so the
  following `mkdir` failed and the case was recorded as "the target was opaque" —
  in 55 of 60 held-out mutants. It now retries the removal and raises with the
  path if a directory survives.
- **The CLI case timeout killed one process, not its tree.** Two orphans from a
  killed pass burned 3 500 s of CPU each and held those directories open, which is
  what caused the lock above. The runner now kills the process group on POSIX and
  the tree with `taskkill /T` on Windows; a test asserts the grandchild is gone.

## Also in this release

- **Bake-off caveats, stated in the report**: it measures tool-call mechanics on a
  ten-test fixture, not refactoring ability on legacy code.
- **Injection scoring fixed**: "call budget exceeded" is neither compliance nor
  resistance, so the report scores what the model did — tests intact, tests
  patched — separately from whether it finished. Result: **0 of 18 runs** patched
  or lost `tests/`.
- **Rate-limit evidence, honestly**: neither provider sends rate-limit headers;
  690 turns, 0 × 429, 32 retries, all recovered within 4 attempts.
- **Tuned and held-out stage-2 rounds** started now that the result is not
  saturated: 7 content fixtures (86-case set, hash `9ddc0ae083a5`) and 60 mutants
  from the 2 005 sites the published sample never touched.

## Reproduce

```
python scripts/build_cases_stage2.py --check
python scripts/mutate.py --target .harness/verify-trees/old --module pycodestyle \
    --cases harness/cases/pycodestyle-cli.json --tests tests --tests-subdir tests \
    --seed 21 --max-mutants 100 --full-tests
python scripts/mutation_audit.py
```
