# Changelog

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
