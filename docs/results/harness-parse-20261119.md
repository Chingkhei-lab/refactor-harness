# Harness baseline - parse 1.22.4

Evidence, not proof: this records what the target did under a fixed case set and a determinism shim. It says nothing about behaviour the cases never reach, and the coverage section below is the honest limit.

| Measurement | Value |
| --- | --- |
| Case set | 273 cases, sha256 `f3b6b66d737ec9bb...` |
| Outcomes | {'return': 238, 'unsupported': 23, 'exception': 12} |
| Opaque / truncated / limit-hit observations | {'opaque': 23, 'truncated': 1, 'limit_hit': 0} |
| A/A passes per order | 5 fixed, 5 shuffled |
| A/A noise (fixed order) | **0** |
| A/A noise (shuffled order) | **0** |
| Cases the shim decides | 3 |
| Line coverage | 425/554 (73.5%) |
| Branch coverage | 163/246 (66.26%) |
| Uncovered functions | 32/47 |
| Golden blobs | 273 |
| Controls applied | clock_frozen, time_time_frozen, struct_time_frozen, monotonic_frozen, random_seeded, uuid_frozen, per_case_reset, hash_seed_0, tz_utc, locale_c, cwd_fixed, env_scrubbed |

## Cases by source

| Source | Cases |
| --- | --- |
| docs | 42 |
| grammar | 132 |
| grammar-random | 33 |
| hypothesis | 40 |
| malformed | 26 |

## Clock surfaces: which ones the shim actually controls

| Surface | Shimmed | Unshimmed | Controlled |
| --- | --- | --- | --- |
| `time.time` | `1700000000.0` | `1795078635.6936731` | yes |
| `time.time_ns` | `1700000000000000000` | `1795078635693673100` | yes |
| `time.monotonic` | `0.0` | `165422.781` | yes |
| `time.perf_counter` | `0.0` | `165422.8046949` | yes |
| `time.localtime` | `time.struct_time(tm_year=2023, tm_mon=11, tm_mday=14, tm_hour=22, tm_min=13, tm_sec=20, tm_wday=1, tm_yday=318, tm_isdst=0)` | `time.struct_time(tm_year=2026, tm_mon=11, tm_mday=19, tm_hour=8, tm_min=57, tm_sec=15, tm_wday=3, tm_yday=323, tm_isdst=0)` | yes |
| `time.gmtime` | `time.struct_time(tm_year=2023, tm_mon=11, tm_mday=14, tm_hour=22, tm_min=13, tm_sec=20, tm_wday=1, tm_yday=318, tm_isdst=0)` | `time.struct_time(tm_year=2026, tm_mon=11, tm_mday=19, tm_hour=8, tm_min=57, tm_sec=15, tm_wday=3, tm_yday=323, tm_isdst=0)` | yes |
| `time.ctime` | `Tue Nov 14 22:13:20 2023` | `Thu Nov 19 08:57:15 2026` | yes |
| `time.strftime` | `2023-11-14 22:13:20` | `2026-11-19 08:57:15` | yes |
| `datetime.now` | `2023-11-14T22:13:20` | `2026-11-19T08:57:15.693673` | yes |
| `datetime.utcnow` | `2023-11-14T22:13:20` | `2026-11-19T08:57:15.693673` | yes |
| `date.today` | `2023-11-14` | `2026-11-19` | yes |
| `random` | `0.8444218515250481` | `0.22894784281920233` | yes |
| `uuid4` | `5eed0000-0000-0000-0000-000000000001` | `fde68280-92d9-4d6c-93e0-064c3b4250dd` | yes |
| `hash` | `-3368204716105132637` | `-3368204716105132637` | **no** |

## A/A noise

- **fixed order**: no case varied across passes.
- **shuffled order**: no case varied across passes.

## Cases the determinism shim decides

Method: run one pass with the shim and one without, then compare the control-free fingerprint of each case. A case that differs is one where a shimmed surface changed the observed value; the clock-surface table above says which surfaces those are.

- `0167-parse-grammar`
- `0169-search-grammar`
- `0170-findall-grammar`

## Known target defect (reproduced outside the harness)

`parse.findall('{:tt}', '22:13:20')` never terminates: after the full match it keeps yielding the same zero-width match at offset 8. Reproduced with no shim, no sandbox and an external timeout, which exits 124. It is NOT new: `parse` 1.22.4 is the latest upstream release and issue #246 ('Avoid infinite findall iteration for empty matches', open since 2026-09-12) tracks it. The harness records the case as a truncated observation (1 000 items, `truncated: true`) instead of hanging.

## Uncovered functions (the limit of these claims)

32 of 47 functions were never executed by the case set:

- `__init__.py::FixedTzOffset.__eq__`
- `__init__.py::FixedTzOffset.__init__`
- `__init__.py::FixedTzOffset.__repr__`
- `__init__.py::FixedTzOffset.dst`
- `__init__.py::FixedTzOffset.tzname`
- `__init__.py::FixedTzOffset.utcoffset`
- `__init__.py::Match.evaluate_result`
- `__init__.py::Parser.__init__`
- `__init__.py::Parser.__repr__`
- `__init__.py::Parser._expand_named_fields`
- `__init__.py::Parser._handle_field`
- `__init__.py::Parser._match_re`
- `__init__.py::Parser._regex_replace`
- `__init__.py::Parser._search_re`
- `__init__.py::Parser._to_group_name`
- `__init__.py::Parser.evaluate_result`
- `__init__.py::Parser.fixed_fields`
- `__init__.py::Parser.format`
- `__init__.py::Parser.named_fields`
- `__init__.py::Parser.search`
- `__init__.py::Result.__contains__`
- `__init__.py::Result.__getitem__`
- `__init__.py::Result.__repr__`
- `__init__.py::ResultIterator.__next__`
- `__init__.py::compile`
- `__init__.py::date_convert`
- `__init__.py::extract_format`
- `__init__.py::int_convert.__call__`
- `__init__.py::percentage`
- `__init__.py::strf_date_convert`
- `__init__.py::with_pattern`
- `__init__.py::with_pattern.decorator`
