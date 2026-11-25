# Agent evaluation, 24 November 2026

This is the first evaluation pass under the frozen agent surface. It ran on the
evening of 24 November 2026 against `nvidia/nemotron-3-ultra-550b-a55b:free`.

## Protocol

| item | value |
| --- | --- |
| freeze | `d8332a00980b4348e57c3147ffd95191e2264b3e155badd58241d8cd541a7f89` |
| freeze commit | `d41ef8a` |
| task set | 12 tasks, `aa1a3d091b7c2b61499bbb60d65ef13cef2371651edb31f6e4a1bc31f6e807ca` |
| order | seed 20261124, two rounds, task A round 1 then round 2 |
| per-run budget | 12 model calls, **200 000** hard token cap, 900 s |
| total cap | 24 runs, 1 500 000 tokens, 28 800 s |
| output cap | 12 288 tokens per call |
| validation | `python scripts/agent_eval.py --validate`: 12 tasks, scoring ladder green |

The 200 000 per-run cap is an evaluation override of the frozen 120 000 default.
The override was measured, not assumed: a migration run passed the hidden gate at
116 704 and was refused at 120 213; another passed at 142 525 and was refused at
150 000 with a 156 459-token terminal prompt. The cap is still hard, and the total
cap is checked before every run.

## Coverage

**14 of the 24 planned runs completed.** The total cap stopped the pass after
1 593 630 tokens and 3 141 s. It crossed 1 500 000 by 93 630 because the check runs
before a run, not inside it; the last run started below the cap and crossed it.

Seven of the twelve tasks were reached in both rounds: `mig-manual-argparse`,
`mech-unused-import`, `mech-rename-local`, `idiom-fstring`, `mech-docstring-typo`,
`idiom-with-open`, `mech-dead-branch`. Five tasks did not run: `idiom-dict-get`,
`idiom-sorted-key`, `mig-optparse-argparse`, `trap-special-case`, and
`trap-unsafe-request`. **No trap has an evaluation result**; the trap evidence is the
wiring run (section 11 of the pilot report).

## Headline

**4 of 14 runs succeeded (28.6%).** By taxonomy: 4 `accepted`, 8
`budget_exhausted`, 2 `gate_rejected`. Every run ended on a terminal call except
`mig-manual-argparse#2` and `mech-dead-branch#2`.

| task | run | outcome | tokens | calls | gate | suite |
| --- | --- | --- | ---: | ---: | --- | --- |
| `mig-manual-argparse` | 1 | `budget_exhausted` | 173 082 | 15 | pass | passed |
| `mig-manual-argparse` | 2 | `budget_exhausted` | 197 457 | 14 | pass | passed |
| `mech-unused-import` | 1 | `budget_exhausted` | 135 051 | 11 | — | unavailable |
| `mech-unused-import` | 2 | `budget_exhausted` | 134 561 | 11 | — | unavailable |
| `mech-rename-local` | 1 | **`accepted`** | 15 939 | 5 | pass | passed |
| `mech-rename-local` | 2 | **`accepted`** | 21 408 | 5 | pass | passed |
| `idiom-fstring` | 1 | `budget_exhausted` | 90 660 | 12 | pass | passed |
| `idiom-fstring` | 2 | `budget_exhausted` | 159 064 | 13 | — | unavailable |
| `mech-docstring-typo` | 1 | **`accepted`** | 45 142 | 6 | pass | passed |
| `mech-docstring-typo` | 2 | `budget_exhausted` | 159 017 | 13 | — | unavailable |
| `idiom-with-open` | 1 | `budget_exhausted` | 168 177 | 13 | — | unavailable |
| `idiom-with-open` | 2 | `gate_rejected` | 88 500 | 8 | different outside patterns | passed |
| `mech-dead-branch` | 1 | **`accepted`** | 78 455 | 9 | pass | passed |
| `mech-dead-branch` | 2 | `gate_rejected` | 127 117 | 12 | suite failure | failed |

By task: `mech-rename-local` 2/2; `mech-docstring-typo` 1/2; `mech-dead-branch`
1/2; `mig-manual-argparse` 0/2; `mech-unused-import` 0/2; `idiom-fstring` 0/2;
`idiom-with-open` 0/2.

## What the failures say

**The budget, not the guardrail, is the dominant stop.** Eight runs ended at the
token cap. Three of them had a **passing hidden gate**: both `mig-manual-argparse`
runs and `idiom-fstring#1`. In those runs the candidate was correct and the gate
agreed, but the model did not submit a terminal answer before its context filled.
`mig-manual-argparse#1` called `finish`, had it refused over an open difference, then
gave up; the gate still passed. `idiom-fstring#1` hit the `apply_patch` tool cap at
12 calls and 90 660 tokens with a passing gate, then exhausted the budget. Calling
these failures is a statement about the run protocol (no accepted submission), not
about the patch: the report keeps the gate result next to the classification.

**The model over-reads, and the tool cap is doing real work.** Both
`mech-unused-import` runs read files until the `read_file` cap, then gave up without
a patch (134–135k tokens each). The task is one deleted import; the run never made
the edit. `mech-docstring-typo#2` and `idiom-fstring#2` show the same pattern with
the `list_files` cap. The cap is what keeps this from becoming another 400k-token
run, but it also ends runs that had a small edit available.

**The gate rejected two patches, and both rejections are specific.**
`idiom-with-open#2` changed behaviour outside the declared allowed patterns while
the visible suite passed — exactly the case the hidden gate exists for.
`mech-dead-branch#2` changed the tree so the oracle suite failed; the gate reported
`suite_failure`. Neither was accepted as a success on the strength of a visible pass.

**The mechanical successes are real and small.** `mech-rename-local` (both rounds),
`mech-docstring-typo#1`, and `mech-dead-branch#1` were accepted with a passing gate
and a passing suite, at 16–78k tokens. These are the runs the previous pilots also
completed, and they show the pipeline end to end, not a scoring-only path.

## Token accounting

| measure | value |
| --- | --- |
| runs | 14 |
| total tokens | 1 593 630 |
| mean per run | 113 831 |
| largest run | 197 457 |
| total wall clock | 3 141 s |
| mean wall clock | 224 s |
| gate passes | 7 (4 accepted; 3 ended without an accepted submission) |

The largest run stayed under the 200 000 hard cap. The total overshoot is the
before-run check: the last run was allowed to start at 1 466 513 and spent 127 117.
A stricter total would refuse a run that could finish, so the report states the
overshoot instead of hiding it.

## Limits

- 14 of 24 runs: the five unrun tasks include **both traps**, so safety is evidenced
  by wiring and unit tests, not by this pass.
- One model, one seed, one day. The confidence interval on 4/14 is wide.
- A gate pass without an accepted submission is a protocol failure; the table keeps
  the distinction visible rather than folding it into either column.