# verify-change - parse

old `C:\Users\yumkh\Documents\Refactor-harness\.harness\target` (1.22.4) vs new `C:\Users\yumkh\Documents\Refactor-harness\.harness\verify-trees\identical` (1.22.4)

Evidence, not proof: this says what these cases did under this shim. It does not say the two versions are equivalent, and the unsupported and coverage lines below are the limit of the claim.

| Measurement | Value |
| --- | --- |
| Case set | 273 cases, sha256 `f3b6b66d737ec9bb...` |
| Order | old, new, old, new (alternating) |
| Rules | 0 rule(s), sha256 `4515107d75a6c14c` |
| Controls | clock_frozen, time_time_frozen, struct_time_frozen, monotonic_frozen, random_seeded, uuid_frozen, per_case_reset, hash_seed_0, tz_utc, locale_c, cwd_fixed, env_scrubbed |
| Budget | wall clock |
| A/A noise (old tree) | 0 |
| identical | 498 |
| equivalent | 0 |
| equivalent_bounded | 2 |
| different | 0 |
| unsupported | 46 |
| flaky | 0 |

## Differences

No case differed.

## Unsupported (counted with unsupported, reasons listed)

- `0044-parse-grammar`: parse was unsupported
- `0048-parse-grammar`: parse was unsupported
- `0052-parse-grammar`: parse was unsupported
- `0056-parse-grammar`: parse was unsupported
- `0060-parse-grammar`: parse was unsupported
- `0064-parse-grammar`: parse was unsupported
- `0068-parse-grammar`: parse was unsupported
- `0072-parse-grammar`: parse was unsupported
- `0076-parse-grammar`: parse was unsupported
- `0084-parse-grammar`: parse was unsupported
- `0092-parse-grammar`: parse was unsupported
- `0096-parse-grammar`: parse was unsupported
- `0100-parse-grammar`: parse was unsupported
- `0104-parse-grammar`: parse was unsupported
- `0108-parse-grammar`: parse was unsupported
- `0112-parse-grammar`: parse was unsupported
- `0116-parse-grammar`: parse was unsupported
- `0120-parse-grammar`: parse was unsupported
- `0132-parse-grammar`: parse was unsupported
- `0152-parse-grammar`: parse was unsupported
- `0156-parse-grammar`: parse was unsupported
- `0168-parse-grammar`: parse was unsupported
- `0172-parse-grammar`: parse was unsupported
- `0044-parse-grammar`: parse was unsupported
- `0048-parse-grammar`: parse was unsupported
- `0052-parse-grammar`: parse was unsupported
- `0056-parse-grammar`: parse was unsupported
- `0060-parse-grammar`: parse was unsupported
- `0064-parse-grammar`: parse was unsupported
- `0068-parse-grammar`: parse was unsupported
- `0072-parse-grammar`: parse was unsupported
- `0076-parse-grammar`: parse was unsupported
- `0084-parse-grammar`: parse was unsupported
- `0092-parse-grammar`: parse was unsupported
- `0096-parse-grammar`: parse was unsupported
- `0100-parse-grammar`: parse was unsupported
- `0104-parse-grammar`: parse was unsupported
- `0108-parse-grammar`: parse was unsupported
- `0112-parse-grammar`: parse was unsupported
- `0116-parse-grammar`: parse was unsupported
- `0120-parse-grammar`: parse was unsupported
- `0132-parse-grammar`: parse was unsupported
- `0152-parse-grammar`: parse was unsupported
- `0156-parse-grammar`: parse was unsupported
- `0168-parse-grammar`: parse was unsupported
- `0172-parse-grammar`: parse was unsupported
