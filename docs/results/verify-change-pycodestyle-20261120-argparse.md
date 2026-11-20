# verify-change - pycodestyle

old `C:\Users\yumkh\Documents\Refactor-harness\.harness\verify-trees\old` (unknown) vs new `C:\Users\yumkh\Documents\Refactor-harness\.harness\verify-trees\new` (unknown)

Evidence, not proof: this says what these cases did under this shim. It does not say the two versions are equivalent, and the unsupported and coverage lines below are the limit of the claim.

| Measurement | Value |
| --- | --- |
| Case set | 61 cases, sha256 `5b86bc8dab70b2f1...` |
| Order | old, new, old, new (alternating) |
| Rules | 0 rule(s), sha256 `84b35c141be426b1` |
| Controls | clock_frozen, time_time_frozen, struct_time_frozen, monotonic_frozen, random_seeded, uuid_frozen, per_case_reset, hash_seed_0, tz_utc, locale_c, cwd_fixed, env_scrubbed |
| Budget | wall clock |
| A/A noise (old tree) | 0 |
| Observation schema | v2 |
| identical | 112 |
| equivalent | 0 |
| equivalent_bounded | 0 |
| different | 10 |
| unsupported | 0 |
| flaky | 0 |

## Differences

| Case | Path | Left | Right |
| --- | --- | --- | --- |
| `probe-blank-lines` | `$.value.stderr` | `Usage: pycodestyle [options] input ...

pycodestyle: error` | `usage: pycodestyle [options] input ...
pycodestyle: error: ` |
| `probe-help` | `$.value.stdout` | `Usage: pycodestyle [options] input ...

Options:
  --vers` | `usage: pycodestyle [options] input ...

positional argumen` |
| `probe-int-error` | `$.value.stderr` | `Usage: pycodestyle [options] input ...

pycodestyle: error` | `usage: pycodestyle [options] input ...
pycodestyle: error: ` |
| `probe-jobs` | `$.value.stderr` | `Usage: pycodestyle [options] input ...

pycodestyle: error` | `usage: pycodestyle [options] input ...
pycodestyle: error: ` |
| `probe-unknown` | `$.value.stderr` | `Usage: pycodestyle [options] input ...

pycodestyle: error` | `usage: pycodestyle [options] input ...
pycodestyle: error: ` |
| `probe-blank-lines` | `$.value.stderr` | `Usage: pycodestyle [options] input ...

pycodestyle: error` | `usage: pycodestyle [options] input ...
pycodestyle: error: ` |
| `probe-help` | `$.value.stdout` | `Usage: pycodestyle [options] input ...

Options:
  --vers` | `usage: pycodestyle [options] input ...

positional argumen` |
| `probe-int-error` | `$.value.stderr` | `Usage: pycodestyle [options] input ...

pycodestyle: error` | `usage: pycodestyle [options] input ...
pycodestyle: error: ` |
| `probe-jobs` | `$.value.stderr` | `Usage: pycodestyle [options] input ...

pycodestyle: error` | `usage: pycodestyle [options] input ...
pycodestyle: error: ` |
| `probe-unknown` | `$.value.stderr` | `Usage: pycodestyle [options] input ...

pycodestyle: error` | `usage: pycodestyle [options] input ...
pycodestyle: error: ` |

## Approved differences (different, and accepted as such)

| Case | Path | Reviewer | Reason |
| --- | --- | --- | --- |
| `probe-blank-lines` | `$.value.stderr` | yumkh (maintainer) | same 'unrecognized arguments' wording for an option that does not exist (--max-blank-lines). Exit status is still 2. |
| `probe-help` | `$.value.stdout` | yumkh (maintainer) | argparse renders usage/help differently: lower-case 'usage:', a positional-arguments section, and 'options:' instead of 'Options:'. The option set and defaults are unchanged; this is the parser swap showing through and cannot be normalised away without hiding it. |
| `probe-int-error` | `$.value.stderr` | yumkh (maintainer) | argparse words an invalid integer differently: 'argument --max-line-length: invalid int value' rather than 'option ... invalid integer value'. Exit status is still 2 and the offending value is still named. |
| `probe-jobs` | `$.value.stderr` | yumkh (maintainer) | an unsupported option name is now reported by argparse's 'unrecognized arguments' path, which echoes the value too (--jobs=1). Exit status is still 2. This case is declared unordered, so only the error text is compared. |
| `probe-unknown` | `$.value.stderr` | yumkh (maintainer) | argparse reports an unknown long option as 'unrecognized arguments: --no-such-option' instead of 'no such option'. Exit status is still 2. |
| `probe-blank-lines` | `$.value.stderr` | yumkh (maintainer) | same 'unrecognized arguments' wording for an option that does not exist (--max-blank-lines). Exit status is still 2. |
| `probe-help` | `$.value.stdout` | yumkh (maintainer) | argparse renders usage/help differently: lower-case 'usage:', a positional-arguments section, and 'options:' instead of 'Options:'. The option set and defaults are unchanged; this is the parser swap showing through and cannot be normalised away without hiding it. |
| `probe-int-error` | `$.value.stderr` | yumkh (maintainer) | argparse words an invalid integer differently: 'argument --max-line-length: invalid int value' rather than 'option ... invalid integer value'. Exit status is still 2 and the offending value is still named. |
| `probe-jobs` | `$.value.stderr` | yumkh (maintainer) | an unsupported option name is now reported by argparse's 'unrecognized arguments' path, which echoes the value too (--jobs=1). Exit status is still 2. This case is declared unordered, so only the error text is compared. |
| `probe-unknown` | `$.value.stderr` | yumkh (maintainer) | argparse reports an unknown long option as 'unrecognized arguments: --no-such-option' instead of 'no such option'. Exit status is still 2. |

## Unsupported (counted with unsupported, reasons listed)

None.
