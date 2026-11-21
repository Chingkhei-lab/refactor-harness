# Changelog

## Unreleased (Phase 5)

### Model routing

- **A model bake-off with a fixed rule and a reproducible report**
  (`docs/results/model-bakeoff-20261122.md`, `scripts/model_bakeoff.py`,
  `scripts/bakeoff_tasks.py`, `scripts/bakeoff_tools.py`,
  `scripts/model_router.py`). 20 tasks in five categories run through the real
  tool layer — `read_file`, `search`, `list_dir`, `apply_patch` with a sha256
  precondition, `run_tests` — against a real workspace copy. Scoring reads the
  final workspace and the tool log only; no model grades another.
  `--self-check` applies each task's setup (the check must fail) and its reference
  fix (the check must pass) before a model is ever measured, and `--analyse`
  rebuilds the report from cassettes with no network call.
- **Result**: primary `nvidia/nemotron-3-ultra-550b-a55b:free` at 93.3 % of 60
  runs (95 % CI 84.1–97.4), fallback `moonshotai/kimi-k3.1-pro:free` at 80.0 %
  (68.2–88.2); 100 % valid tool calls for all three finalists. `gemini-3.8-flash`
  passes both smoke turns as the provider-level fallback, at 460 s for one call on
  the free tier.
- **A provider adapter that keeps what the provider needs echoed.** Gemini 3.x
  returns a `thought_signature` under `tool_calls[i].extra_content.google` and
  answers HTTP 400 on the next turn if it is dropped, so `assistant_message()`
  replays any extra field instead of rebuilding an OpenAI-shaped message.
- **Retries cover every 5xx.** The first pass of the bake-off was thrown away
  because the policy listed only `{500, 502, 503, 504}` and the provider's edge
  answers 520/522: the most common failure was classified correctly and never
  retried. Twelve of the failures in that pass were a single 520 on turn one.


## Unreleased (Phase 4, checkpoint 12b audit)

### Harness

- **A mutant is one contiguous text edit, not a re-print of the module.**
  `apply_site` returned `ast.unparse(tree)`: every mutant of `pycodestyle.py`
  rewrote 2 717 lines into 2 095, so every line number and echoed source line
  moved. Two CLI cases print a traceback and a warning that name the target's own
  file and line, so 77 and 75 of 100 mutants differed on them and 38 differed on
  nothing else — a 100 % kill rate that measured the mutator. Mutations are now
  splices on the original bytes, and `mutate()` refuses a result that touches more
  than one contiguous region on the site's line. Re-running stage 1 with the fixed
  mutator reproduced **all 300 verdicts**, so the stage-1 numbers stand; re-running
  stage 2 gives **harness 62/100 = 62.0 % and suite 74/100 = 74.0 %**, with 4
  harness-only kills.
- **A CLI observation cannot contain the target's source.**
  `harness.projections.canonicalise_stream` keeps a traceback's exception type,
  message and frame names and drops the path, line number, echoed source line and
  caret line, and folds CRLF to LF. Two approvals (`probe-config-int-error`,
  `probe-deprecated-section`) that existed only because a line number moved are
  gone. `observation_schema_version` is now **3**.
- **An api snapshot may not contain the path it was imported from.** The no-op
  `comment` canary reported two differences on Linux: `pycodestyle.USER_CONFIG` is
  `expanduser('~/.config/pycodestyle')`, evaluated at import, so it is the path of
  the copy the child imported — and the canary compares a tree with a copy at a
  different path. `HOME`, `XDG_CONFIG_HOME`, `TMPDIR`/`TEMP`/`TMP`, the workspace
  and the case directory are replaced by markers (`<home>`, `<tmp>`, `<cwd>`, …),
  longest root first, and the markers appear in the observation's controls. An A/A
  run cannot see this, because both passes use one path; a canary can.
- **New case kind `api`.** An api case imports the module and snapshots its public
  surface — names, `inspect.signature`, class bases and attributes, stable scalars,
  first docstring line — plus, when it names a call, the surface of what that call
  returns. `get_parser().add_option` is no longer a suite-only finding; it is an
  approved difference with a witness (`OptionParser` → `ArgumentParser`).
- **`scripts/mutation_audit.py`** classifies every mutant: was its line reached
  (statement-span aware), how many cases differ, which tests failed, and whether a
  suite kill was cosmetic (`test_all.py::test_own_dog_food` runs the tool over its
  own source). `scripts/mutation_canary.py` runs four behaviour-preserving mutants
  through both oracles and is a CI step.
- **`scripts/mutant_evidence.py`** prints one mutant's evidence: the changed
  region, the cases that differ, a diff of the first differing field, the suite's
  failing test ids.
- **The stage-1 case set is reproducible.** The 40 `hypothesis`-collected inputs
  are frozen as data: regenerating them produced three different case-set hashes
  from the same library version. `harness_baseline.py` now verifies the committed
  case file and refuses to overwrite it without `--write-cases`, and a test asserts
  that regeneration reproduces the committed hash. The stage-1 set is
  `ed283159d51e35b3` (273 cases); `283155b0bbb9` is superseded.
- **`mutate.py` guards its own oracle**: a test run that collected nothing (a path
  that does not exist inside the mutant copy) used to become "killed" for every
  mutant, and a resumed row recorded without the test oracle is now re-run instead
  of being mixed into a report.
- **`stage2_coverage.py --lines-out`** and `harness_baseline.py --lines-out` write
  the executed line numbers, so a mutation audit can ask whether a mutated line ran
  at all.


## Unreleased (Phase 4, checkpoint 12a)

### Harness

- **`observation_schema_version: 2`.** The `EncodedValue` fix (checkpoint 11)
  changed every observation's bytes without changing any observed value: the
  parent was encoding values the child had already encoded, so each tag was
  escaped through the `__mapping__` form. The version is now in every
  observation, in every report and in the blob path
  (`<root>/v2/<case_set_hash[:8]>/<sha[:2]>/<sha>.json`), so v1 and v2 blobs
  cannot be mixed.
  Verified: regenerating the checkpoint-10 case set under schema v2 reproduced
  the headline numbers exactly — **238 return / 12 exception / 23 unsupported,
  273 blobs** (`docs/results/harness-parse-20261119-schema-v2.md`).
- **A target crash is a behaviour, not a harness bug.** A child that exits with a
  status or dies on a signal other than SIGXCPU/SIGKILL is now recorded as an
  observation with `{"__process__": {kind, exit_code, signal, stderr_tail}}` and
  compared like any other value. Budget kills (SIGXCPU) and resource kills
  (SIGKILL) remain `unsupported`; "harness bug" now means only "died before any
  begin marker". A target that cannot be imported is recorded as an import
  failure for every case.
- **Case budgets.** `RLIMIT_CPU` soft limit per case; the pass-wide hard limit is
  `2 × per_case × cases` (floor 60 s), set once because POSIX cannot raise a hard
  limit again. A pass that exhausts its three child restarts records the
  unreached cases as `unsupported (NotRun)` instead of dropping them.
- **Rules.** Quoted path segments (`$.value["a.b"]`) for keys containing the
  separator; per-rule usage statistics (matches, largest observed difference
  versus tolerance) in every report.
- **Projections.** A named, allowlisted view (`harness/projections.py`, selected
  by `projection` in the case spec) makes `parse`'s `evaluate_result=False` path
  observable: **0 unsupported** (was 23), 261 return / 12 exception.
- **`expect: finite | stream`.** A stream may be truncated (two matching prefixes
  are `equivalent_bounded`); a finite case that hits the item limit is
  `unsupported (LimitHit)`.
- **Mutation testing** (`harness.mutate`, `scripts/mutate.py`) with seven
  operators, deterministic site enumeration, seeded stratified sampling, and
  classification of survivors; **refactor control group**
  (`scripts/refactor_check.py`) with 12 harmless refactors and one seeded bug.

## Unreleased — checkpoint 12a

### The harness

- **A target crash is an observation, not a harness bug.** Exit status, signal,
  and an import failure are recorded under `__process__` / `__exception__` and
  compared. Only `SIGXCPU` (per-case CPU budget) and `SIGKILL` (resource) are
  `unsupported`; a death with no begin marker is the only harness bug.
- **Observation schema v2.** `observation_schema_version` is in every
  observation, every report and every blob path (`<root>/v2/<hash8>/…`). The
  checkpoint-10 case set regenerated under v2 reproduces 238 return / 12
  exception / 23 unsupported and 273 blobs.
- **A pass-wide hard CPU limit**, `max(60, 2 × per_case × cases)` = 5460 s for
  10 s × 273 cases, set once because POSIX can never raise a hard limit again.
  Restarts and signal kills count against the three-attempt resume limit;
  cases never reached are recorded `unsupported (NotRun)`.
- **Per-case stall watchdog** in place of a pass-wide wall timeout: a hanging
  mutant is bounded and the pass resumes (a pass-wide timeout let one hanging
  mutant run for hours during the first mutation run).
- **Rule usage** per rule in the report: matches, largest observed difference,
  tolerance and the ratio, so a rule that masks everything is visible.
- **Quoted path segments** — `$.value["a.b"]` — while an unquoted segment that
  contains a dot is still refused.
- **Projections**: an allowlisted named view (`identity`, `evaluate_result`,
  `result_fields`) replaces opening the target up to case specs. With
  `evaluate_result` the case set goes from 23 unsupported to **0**, 261 return /
  12 exception, line coverage 426/554.
- **`expect: finite | stream`** on every case: a finite case that hits the
  encoding limit is a failure (`unsupported (LimitHit)`), a stream case is
  `equivalent_bounded`.

### Mutation testing (12a)

- `src/harness/mutate.py` — seven operator families (eight operators), one change
  per mutant, deterministic depth-first site enumeration, every mutant re-parsed
  before use. 839 sites in `parse/__init__.py`.
- `scripts/mutate.py` — timing probe, seeded stratified sample, plan written
  before any result, harness and upstream-test oracles, resumable `results.jsonl`.
- `scripts/mutation_analysis.py` — splits survivors by line coverage and merges
  the hand verdicts in `docs/results/mutation-verdicts.json`.
- `scripts/refactor_check.py` — twelve harmless refactors and one seeded bug.
- 300 mutants (seed 12): **139 killed by the harness (46.3 %), 42 by the upstream
  suite (14.0 %), 181 by either (60.3 %)**; on covered lines 138/203 = 68.0 %,
  10/203 = 4.9 %, 148/203 = 72.9 %. Survivors: 96 not covered, 63
  covered-not-observed, 2 equivalent.
- Harmless refactors: 12/12 clean, 0 false positives. A `"-"` → `"+"` sign flip
  hidden inside a refactor was `different` on 4 cases.

### CI

- Both jobs publish their JUnit counts as `::notice` annotations and as a commit
  status, because job logs need admin rights and the uploaded artifacts need a
  token. A red run also names the failing tests in a `::error` annotation.

## Unreleased — 12a corrections

### The test oracle was inverted

`_run_tests` returned `returncode == 0` and the caller stored it as `tests_killed`,
so a *passing* suite was recorded as a kill. Every 12a number derived from it was
wrong. Corrected from the same 300 rows, each row's pytest note asserted against
the flipped value before the migration was written:

| | Tests killed it | Tests missed it |
| --- | --- | --- |
| Harness killed it | 139 | 0 |
| Harness missed it | 120 | 41 |

Harness 46.3 %, upstream 86.3 %, union 86.3 % — the harness is a **subset** of the
suite, not an addition to it. The two "equivalent" survivors were also refuted by
this cross-check (one is killed by the suite), so the sample has **no** equivalent
mutant and all 65 covered survivors are case-set gaps.

### Everything is now interval-shaped

`_rate` prints a Wilson 95 % interval beside each proportion and the sampling
method (stratified, seed) is stated in the plan and the report.

### Tuning protocol

- 34 cases added for the seven survivor clusters (`harness/cases/parse-tuned.json`,
  307 cases, hash `4dcdd838`).
- First pass unchanged as the headline: 139/300 = 46.3 %.
- Tuned on the same 300 (overfit): 180/300 = 60.0 %; 41 newly killed, 0 lost.
- Held-out, 150 mutants from the 539 untouched sites (seed 99): 58/150 = 38.7 %
  with the original cases, 83/150 = 55.3 % with the tuned ones. The gain
  generalises; `docs/results/mutation-tuning-20261120.md`.

### Other

- The mutator test no longer needs the git-ignored `.harness/` tree: a vendored
  fixture (`tests/fixtures/mutate_module.py`) is mutated at every site on every
  run, and a second test asserts the fixture reaches all eight operators.
- `mutation-parse-20261119.md` gains a worked example of the one kill on a line
  the cases never execute (`bool_flip@406:8`: making a guard true turns a
  never-taken `raise` into a taken one, so the mutant changes which lines run).
- `refactors-20261119.md`: one row per refactor with its class counts, plus the
  false-positive count and the seeded-bug control.
