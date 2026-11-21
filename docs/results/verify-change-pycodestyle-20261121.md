# verify-change - pycodestyle

old `C:\Users\yumkh\Documents\Refactor-harness\.harness\verify-trees\old` (unknown) vs new `C:\Users\yumkh\Documents\Refactor-harness\.harness\verify-trees\new` (unknown)

Evidence, not proof: this says what these cases did under this shim. It does not say the two versions are equivalent, and the unsupported and coverage lines below are the limit of the claim.

| Measurement | Value |
| --- | --- |
| Case set | 79 cases, sha256 `8f04551c95d70c5f...` |
| Order | old, new, old, new (alternating) |
| Rules | 0 rule(s), sha256 `708326b60b05d9ce` |
| Controls | clock_frozen, time_time_frozen, struct_time_frozen, monotonic_frozen, random_seeded, uuid_frozen, per_case_reset, hash_seed_0, tz_utc, locale_c, cwd_fixed, env_scrubbed, cli_stream_projected |
| Budget | wall clock |
| A/A noise (old tree) | 0 |
| Observation schema | v3 |
| identical | 142 |
| equivalent | 0 |
| equivalent_bounded | 0 |
| different | 16 |
| unsupported | 0 |
| flaky | 0 |

## Differences

| Case | Path | Left | Right |
| --- | --- | --- | --- |
| `api-module` | `$.value.classes.OptionParser` | `{'attributes': {'add_option': {'callable': '(self, *args, **` | `None` |
| `api-parser` | `$.value.call.attributes.add_argument` | `None` | `{'callable': '(*args, **kwargs)'}` |
| `probe-blank-lines` | `$.value.stderr` | `Usage: pycodestyle [options] input ...

pycodestyle: error: ` | `usage: pycodestyle [options] input ...
pycodestyle: error: u` |
| `probe-help` | `$.value.stdout` | `Usage: pycodestyle [options] input ...

Options:
  --version` | `usage: pycodestyle [options] input ...

positional arguments` |
| `probe-int-error` | `$.value.stderr` | `Usage: pycodestyle [options] input ...

pycodestyle: error: ` | `usage: pycodestyle [options] input ...
pycodestyle: error: a` |
| `probe-jobs` | `$.value.stderr` | `Usage: pycodestyle [options] input ...

pycodestyle: error: ` | `usage: pycodestyle [options] input ...
pycodestyle: error: u` |
| `probe-no-args` | `$.value.stderr` | `Usage: pycodestyle [options] input ...

pycodestyle: error: ` | `usage: pycodestyle [options] input ...
pycodestyle: error: i` |
| `probe-unknown` | `$.value.stderr` | `Usage: pycodestyle [options] input ...

pycodestyle: error: ` | `usage: pycodestyle [options] input ...
pycodestyle: error: u` |
| `api-module` | `$.value.classes.OptionParser` | `{'attributes': {'add_option': {'callable': '(self, *args, **` | `None` |
| `api-parser` | `$.value.call.attributes.add_argument` | `None` | `{'callable': '(*args, **kwargs)'}` |
| `probe-blank-lines` | `$.value.stderr` | `Usage: pycodestyle [options] input ...

pycodestyle: error: ` | `usage: pycodestyle [options] input ...
pycodestyle: error: u` |
| `probe-help` | `$.value.stdout` | `Usage: pycodestyle [options] input ...

Options:
  --version` | `usage: pycodestyle [options] input ...

positional arguments` |
| `probe-int-error` | `$.value.stderr` | `Usage: pycodestyle [options] input ...

pycodestyle: error: ` | `usage: pycodestyle [options] input ...
pycodestyle: error: a` |
| `probe-jobs` | `$.value.stderr` | `Usage: pycodestyle [options] input ...

pycodestyle: error: ` | `usage: pycodestyle [options] input ...
pycodestyle: error: u` |
| `probe-no-args` | `$.value.stderr` | `Usage: pycodestyle [options] input ...

pycodestyle: error: ` | `usage: pycodestyle [options] input ...
pycodestyle: error: i` |
| `probe-unknown` | `$.value.stderr` | `Usage: pycodestyle [options] input ...

pycodestyle: error: ` | `usage: pycodestyle [options] input ...
pycodestyle: error: u` |

## Approved differences (different, and accepted as such)

| Case | Path | Reviewer | Reason |
| --- | --- | --- | --- |
| `api-module` | `$.value.classes.OptionParser` | yumkh (maintainer) | The module stops exporting `OptionParser` and starts exporting `argparse`: the migration replaces optparse's OptionParser with argparse's ArgumentParser, and the class surface (`add_option`, `add_options`, `disable_interspersed_args`, ...) goes with it. Approved as a public-surface change and reported as such, never counted as `equivalent`. Before the api cases existed this row was invisible to the harness and appeared only as the upstream suite's `test_read_config_prefix` failure. |
| `api-parser` | `$.value.call.attributes.add_argument` | yumkh (maintainer) | `get_parser()` now returns an `ArgumentParser`: `add_option`, `add_options` and `add_option_group` are gone and `add_argument`/`add_argument_group` take their place. This is the same break the upstream suite reports as `AttributeError: 'ArgumentParser' object has no attribute 'add_option'`, and it is inherent to using argparse. Approved with a witness, so a reader can see the exact attributes that changed. |
| `probe-blank-lines` | `$.value.stderr` | yumkh (maintainer) | same 'unrecognized arguments' wording for an option that does not exist (--max-blank-lines). Exit status is still 2. |
| `probe-help` | `$.value.stdout` | yumkh (maintainer) | argparse renders usage/help differently: lower-case 'usage:', a positional-arguments section, and 'options:' instead of 'Options:'. The option set and defaults are unchanged; this is the parser swap showing through and cannot be normalised away without hiding it. |
| `probe-int-error` | `$.value.stderr` | yumkh (maintainer) | argparse words an invalid integer differently: 'argument --max-line-length: invalid int value' rather than 'option ... invalid integer value'. Exit status is still 2 and the offending value is still named. |
| `probe-jobs` | `$.value.stderr` | yumkh (maintainer) | an unsupported option name is now reported by argparse's 'unrecognized arguments' path, which echoes the value too (--jobs=1). Exit status is still 2. This case is declared unordered, so only the error text is compared. |
| `probe-no-args` | `$.value.stderr` | yumkh (maintainer) | Empty command line: argparse prints 'usage:' in lower case and omits the blank line before 'error:', so `pycodestyle: error: input not specified` is preceded by different text. The error line itself, the exit status (2) and the condition are unchanged. |
| `probe-unknown` | `$.value.stderr` | yumkh (maintainer) | argparse reports an unknown long option as 'unrecognized arguments: --no-such-option' instead of 'no such option'. Exit status is still 2. |
| `api-module` | `$.value.classes.OptionParser` | yumkh (maintainer) | The module stops exporting `OptionParser` and starts exporting `argparse`: the migration replaces optparse's OptionParser with argparse's ArgumentParser, and the class surface (`add_option`, `add_options`, `disable_interspersed_args`, ...) goes with it. Approved as a public-surface change and reported as such, never counted as `equivalent`. Before the api cases existed this row was invisible to the harness and appeared only as the upstream suite's `test_read_config_prefix` failure. |
| `api-parser` | `$.value.call.attributes.add_argument` | yumkh (maintainer) | `get_parser()` now returns an `ArgumentParser`: `add_option`, `add_options` and `add_option_group` are gone and `add_argument`/`add_argument_group` take their place. This is the same break the upstream suite reports as `AttributeError: 'ArgumentParser' object has no attribute 'add_option'`, and it is inherent to using argparse. Approved with a witness, so a reader can see the exact attributes that changed. |
| `probe-blank-lines` | `$.value.stderr` | yumkh (maintainer) | same 'unrecognized arguments' wording for an option that does not exist (--max-blank-lines). Exit status is still 2. |
| `probe-help` | `$.value.stdout` | yumkh (maintainer) | argparse renders usage/help differently: lower-case 'usage:', a positional-arguments section, and 'options:' instead of 'Options:'. The option set and defaults are unchanged; this is the parser swap showing through and cannot be normalised away without hiding it. |
| `probe-int-error` | `$.value.stderr` | yumkh (maintainer) | argparse words an invalid integer differently: 'argument --max-line-length: invalid int value' rather than 'option ... invalid integer value'. Exit status is still 2 and the offending value is still named. |
| `probe-jobs` | `$.value.stderr` | yumkh (maintainer) | an unsupported option name is now reported by argparse's 'unrecognized arguments' path, which echoes the value too (--jobs=1). Exit status is still 2. This case is declared unordered, so only the error text is compared. |
| `probe-no-args` | `$.value.stderr` | yumkh (maintainer) | Empty command line: argparse prints 'usage:' in lower case and omits the blank line before 'error:', so `pycodestyle: error: input not specified` is preceded by different text. The error line itself, the exit status (2) and the condition are unchanged. |
| `probe-unknown` | `$.value.stderr` | yumkh (maintainer) | argparse reports an unknown long option as 'unrecognized arguments: --no-such-option' instead of 'no such option'. Exit status is still 2. |

## Unsupported (counted with unsupported, reasons listed)

None.
