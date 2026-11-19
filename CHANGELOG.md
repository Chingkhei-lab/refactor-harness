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
