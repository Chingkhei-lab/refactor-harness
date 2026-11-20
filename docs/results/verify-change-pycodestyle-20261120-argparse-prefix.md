# verify-change - pycodestyle

old `C:\Users\yumkh\Documents\Refactor-harness\.harness\verify-trees\old` (unknown) vs new `C:\Users\yumkh\Documents\Refactor-harness\.harness\verify-trees\prefix` (unknown)

Evidence, not proof: this says what these cases did under this shim. It does not say the two versions are equivalent, and the unsupported and coverage lines below are the limit of the claim.

| Measurement | Value |
| --- | --- |
| Case set | 77 cases, sha256 `be70731d8c084343...` |
| Order | old, new, old, new (alternating) |
| Rules | 0 rule(s), sha256 `3a52f3abfc1d5f9a` |
| Controls | clock_frozen, time_time_frozen, struct_time_frozen, monotonic_frozen, random_seeded, uuid_frozen, per_case_reset, hash_seed_0, tz_utc, locale_c, cwd_fixed, env_scrubbed |
| Budget | wall clock |
| A/A noise (old tree) | 0 |
| Observation schema | v2 |
| identical | 136 |
| equivalent | 0 |
| equivalent_bounded | 0 |
| different | 18 |
| unsupported | 0 |
| flaky | 0 |

## Differences

| Case | Path | Left | Right |
| --- | --- | --- | --- |
| `probe-blank-lines` | `$.value.stderr` | `Usage: pycodestyle [options] input ...

pycodestyle: error` | `usage: pycodestyle [options] input ...
pycodestyle: error: ` |
| `probe-config-int-error` | `$.value.stderr` | `Traceback (most recent call last):
  File "C:\Users\yumkh\D` | `Traceback (most recent call last):
  File "C:\Users\yumkh\D` |
| `probe-deprecated-section` | `$.value.stderr` | `C:\Users\yumkh\Documents\Refactor-harness\.harness\verify\tr` | `C:\Users\yumkh\Documents\Refactor-harness\.harness\verify\tr` |
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
| `probe-no-args` | `$.value.stderr` | `Usage: pycodestyle [options] input ...

pycodestyle: error` | `usage: pycodestyle [options] input ...
pycodestyle: error: ` |
| `probe-unknown` | `$.value.stderr` | `Usage: pycodestyle [options] input ...

pycodestyle: error` | `usage: pycodestyle [options] input ...
pycodestyle: error: ` |
| `probe-version` | `$.value.stdout` | `2.15.0
` | `pycodestyle 2.15.0
` |
| `probe-blank-lines` | `$.value.stderr` | `Usage: pycodestyle [options] input ...

pycodestyle: error` | `usage: pycodestyle [options] input ...
pycodestyle: error: ` |
| `probe-config-int-error` | `$.value.stderr` | `Traceback (most recent call last):
  File "C:\Users\yumkh\D` | `Traceback (most recent call last):
  File "C:\Users\yumkh\D` |
| `probe-deprecated-section` | `$.value.stderr` | `C:\Users\yumkh\Documents\Refactor-harness\.harness\verify\tr` | `C:\Users\yumkh\Documents\Refactor-harness\.harness\verify\tr` |
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
| `probe-no-args` | `$.value.stderr` | `Usage: pycodestyle [options] input ...

pycodestyle: error` | `usage: pycodestyle [options] input ...
pycodestyle: error: ` |
| `probe-unknown` | `$.value.stderr` | `Usage: pycodestyle [options] input ...

pycodestyle: error` | `usage: pycodestyle [options] input ...
pycodestyle: error: ` |
| `probe-version` | `$.value.stdout` | `2.15.0
` | `pycodestyle 2.15.0
` |

## Approved differences (different, and accepted as such)

| Case | Path | Reviewer | Reason |
| --- | --- | --- | --- |
| `probe-blank-lines` | `$.value.stderr` | yumkh (maintainer) | same 'unrecognized arguments' wording for an option that does not exist (--max-blank-lines). Exit status is still 2. |
| `probe-help` | `$.value.stdout` | yumkh (maintainer) | argparse renders usage/help differently: lower-case 'usage:', a positional-arguments section, and 'options:' instead of 'Options:'. The option set and defaults are unchanged; this is the parser swap showing through and cannot be normalised away without hiding it. |
| `probe-int-error` | `$.value.stderr` | yumkh (maintainer) | argparse words an invalid integer differently: 'argument --max-line-length: invalid int value' rather than 'option ... invalid integer value'. Exit status is still 2 and the offending value is still named. |
| `probe-jobs` | `$.value.stderr` | yumkh (maintainer) | an unsupported option name is now reported by argparse's 'unrecognized arguments' path, which echoes the value too (--jobs=1). Exit status is still 2. This case is declared unordered, so only the error text is compared. |
| `probe-no-args` | `$.value.stderr` | yumkh (maintainer) | Empty command line: argparse prints 'usage:' in lower case and omits the blank line before 'error:', so `pycodestyle: error: input not specified` is preceded by different text. The error line itself, the exit status (2) and the condition are unchanged. |
| `probe-unknown` | `$.value.stderr` | yumkh (maintainer) | argparse reports an unknown long option as 'unrecognized arguments: --no-such-option' instead of 'no such option'. Exit status is still 2. |
| `probe-blank-lines` | `$.value.stderr` | yumkh (maintainer) | same 'unrecognized arguments' wording for an option that does not exist (--max-blank-lines). Exit status is still 2. |
| `probe-help` | `$.value.stdout` | yumkh (maintainer) | argparse renders usage/help differently: lower-case 'usage:', a positional-arguments section, and 'options:' instead of 'Options:'. The option set and defaults are unchanged; this is the parser swap showing through and cannot be normalised away without hiding it. |
| `probe-int-error` | `$.value.stderr` | yumkh (maintainer) | argparse words an invalid integer differently: 'argument --max-line-length: invalid int value' rather than 'option ... invalid integer value'. Exit status is still 2 and the offending value is still named. |
| `probe-jobs` | `$.value.stderr` | yumkh (maintainer) | an unsupported option name is now reported by argparse's 'unrecognized arguments' path, which echoes the value too (--jobs=1). Exit status is still 2. This case is declared unordered, so only the error text is compared. |
| `probe-no-args` | `$.value.stderr` | yumkh (maintainer) | Empty command line: argparse prints 'usage:' in lower case and omits the blank line before 'error:', so `pycodestyle: error: input not specified` is preceded by different text. The error line itself, the exit status (2) and the condition are unchanged. |
| `probe-unknown` | `$.value.stderr` | yumkh (maintainer) | argparse reports an unknown long option as 'unrecognized arguments: --no-such-option' instead of 'no such option'. Exit status is still 2. |

**Stale approvals** (recorded diff no longer matches; counted as different):

- `probe-config-int-error` `$.value.stderr`: expected `8b131eae9d88`, observed `cfda42ecc587`
- `probe-deprecated-section` `$.value.stderr`: expected `bd981c645139`, observed `4a19d381b05e`
- `probe-config-int-error` `$.value.stderr`: expected `8b131eae9d88`, observed `cfda42ecc587`
- `probe-deprecated-section` `$.value.stderr`: expected `bd981c645139`, observed `4a19d381b05e`

## Unsupported (counted with unsupported, reasons listed)

None.
