# Agent evaluation runner — specification

Status: specified 2026-11-24. `scripts/agent_eval.py` is the implementation; the plan
is committed here first so the runner cannot quietly invent it.

## Configuration

No hardcoded tree in the runner. Each task names a target, and the target config
supplies:

| field | mccabe | pycodestyle |
| --- | --- | --- |
| pinned tree | `.harness/oracle/mccabe` | `.harness/oracle/pycodestyle` |
| visible cases | `harness/cases/mccabe.json` | `harness/cases/pycodestyle-cli.json` |
| hidden cases | `harness/cases/mccabe-hidden.json` | `harness/cases/pycodestyle-cli-hidden.json` |
| rules | `harness/rules/mccabe.json` | `harness/rules/pycodestyle.json` |
| hidden rules | `harness/rules/mccabe-hidden.json` | `harness/rules/pycodestyle-hidden.json` |
| goldens | `harness/golden/mccabe-*.json` | `harness/golden/pycodestyle-*.json` |
| suite | mccabe's pytest command with the documented deselect | `pytest -q tests` from the tree root |

The suite command, cwd globs and timeout are target data, not switches in the run
function. A target missing any field is a configuration error, raised before staging.

## One run record (JSON Lines)

`run_key` (task, repeat, model), `run_id`, seed, model id, target, outcome, taxonomy
class and reason, tokens, wall seconds, `reached_wrapup`, `wrapup_reason`,
`forced_terminal`, `finished_open_difference`, gate category and pass, suite status,
hidden result, guardrail refusal codes, and the cassette directory.

The cassette directory is `.harness/agent-eval/<run_id>/`; `cassette.jsonl` is written
before a response is parsed.

## Scheduling and caps

- Order: two rounds; each round shuffles the 12 tasks with a recorded seed, then runs
  the rounds interleaved (task A round 1, task A round 2, ... as the seeded shuffle
  says), never all of one round first.
- Resume: read the JSONL, skip every `run_key` already present, and continue at the
  recorded seed.
- Caps checked **before** each run: total tokens, wall clock, and run count. On a cap,
  write a summary naming completed and skipped tasks; never start a run that would
  obviously exceed the token cap.
- The total cap stays 1 500 000 for the first pass. At the measured mean it may cover
  about 15 of 24 runs; coverage is reported, not extrapolated.

## Validation before any model tokens

For each of the 12 tasks, drive the scoring path with a scripted observation and assert:

1. known-good patch: gate `pass`, suite pass → accepted, success.
2. behaviour-changing patch: gate `different_outside_allowed_patterns` → gate
   rejected, failure.
3. special-casing patch: guardrail `copied_case_literal` or hidden gate rejection →
   rejected, failure.
4. empty candidate: no patch → `no_patch`.
5. migration patch: allowed differences, gate `pass` → accepted, success.

No provider is called in this step. The runner exposes `--validate` and exits nonzero
on the first mismatch.

## Wiring runs

Three real-model runs, one each: a mechanical mccabe task, the pycodestyle migration,
and the special-casing trap. They are tagged `wiring`, excluded from the headline, and
must each end in a terminal call before the evaluation starts. Their records go to
`docs/results/agent-wiring.jsonl`.

## Launch

`python scripts/agent_eval.py --run --seed <n> --out docs/results/agent-eval.jsonl`
runs in the background. The freeze check must pass first; no prompt, tool, budget or
guardrail edit is allowed while it runs.
