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
| Cases whose value moves when the frozen epoch moves a year | 3 |
| Line coverage | 425/554 (76.71%) |
| Branch coverage | 163/246 (66.26%) |
| Combined (what coverage.py reports as `percent_covered`) | 73.5% |
| Functions never entered | 19/47 (29 statements) |
| Functions with an unexecuted line | 32/47 |
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

Both arms are real runs of the probe: one with the shim module injected, one without. A surface is 'controlled' only if the two arms differ.

`hash` shows as uncontrolled because it is fixed in the *environment* (`PYTHONHASHSEED=0`, read before any of our code runs) rather than by the injected module, so both arms agree. It is still controlled; the probe just cannot see the difference.

| Surface | Shimmed | Unshimmed | Controlled |
| --- | --- | --- | --- |
| `time.time` | `1700000000.0` | `1795105958.3841047` | yes |
| `time.time_ns` | `1700000000000000000` | `1795105958384104600` | yes |
| `time.monotonic` | `0.0` | `192745.468` | yes |
| `time.perf_counter` | `0.0` | `192745.4958289` | yes |
| `time.localtime` | `time.struct_time(tm_year=2023, tm_mon=11, tm_mday=14, tm_hour=22, tm_min=13, tm_sec=20, tm_wday=1, tm_yday=318, tm_isdst=0)` | `time.struct_time(tm_year=2026, tm_mon=11, tm_mday=19, tm_hour=16, tm_min=32, tm_sec=38, tm_wday=3, tm_yday=323, tm_isdst=0)` | yes |
| `time.gmtime` | `time.struct_time(tm_year=2023, tm_mon=11, tm_mday=14, tm_hour=22, tm_min=13, tm_sec=20, tm_wday=1, tm_yday=318, tm_isdst=0)` | `time.struct_time(tm_year=2026, tm_mon=11, tm_mday=19, tm_hour=16, tm_min=32, tm_sec=38, tm_wday=3, tm_yday=323, tm_isdst=0)` | yes |
| `time.ctime` | `Tue Nov 14 22:13:20 2023` | `Thu Nov 19 16:32:38 2026` | yes |
| `time.strftime` | `2023-11-14 22:13:20` | `2026-11-19 16:32:38` | yes |
| `datetime.now` | `2023-11-14T22:13:20` | `2026-11-19T16:32:38.384104` | yes |
| `datetime.utcnow` | `2023-11-14T22:13:20` | `2026-11-19T16:32:38.384104` | yes |
| `date.today` | `2023-11-14` | `2026-11-19` | yes |
| `random` | `0.8444218515250481` | `0.1995073064524432` | yes |
| `uuid4` | `5eed0000-0000-0000-0000-000000000001` | `b56528bc-b4b8-45b0-ab7e-75e6e81ee3f8` | yes |
| `hash` | `-3368204716105132637` | `-3368204716105132637` | **no** |

## A/A noise

- **fixed order**: no case varied across passes.
- **shuffled order**: no case varied across passes.

## Cases that read the clock

Method: run the same case set twice with the shim **on**, once at the default frozen epoch and once a year later, and compare fingerprints. This is the experiment that works: two shim-OFF passes on the same day agree even for time-dependent cases, because the real date is the same in both, so shim-off agreement is not evidence of clock independence. Moving the frozen epoch is.

- `0167-parse-grammar`
- `0169-search-grammar`
- `0170-findall-grammar`

## Known target defect (reproduced outside the harness)

`parse.findall('{:tt}', '22:13:20')` never terminates: after the full match it keeps yielding the same zero-width match at offset 8. Reproduced with no shim, no sandbox and an external timeout, which exits 124. It is NOT new: `parse` 1.22.4 is the latest upstream release and issue #246 ('Avoid infinite findall iteration for empty matches', open since 2026-09-12) tracks it. The harness records the case as a truncated observation (1 000 items, `truncated: true`) instead of hanging.

## Function coverage (the limit of these claims)

19 of 47 functions were never entered (29 of 554 statements); another 13 were entered but have an unexecuted line. The count is coverage.py's own function data (it parses the source), not a separate AST walk, and 'uncovered' here means at least one unexecuted line.

| Function | Start | Lines run | Lines not run |
| --- | --- | --- | --- |
| `__init__.py::FixedTzOffset.__eq__` | 138 | 0 | 3 |
| `__init__.py::FixedTzOffset.__init__` | 122 | 0 | 2 |
| `__init__.py::FixedTzOffset.__repr__` | 126 | 0 | 1 |
| `__init__.py::FixedTzOffset.dst` | 135 | 0 | 1 |
| `__init__.py::FixedTzOffset.tzname` | 132 | 0 | 1 |
| `__init__.py::FixedTzOffset.utcoffset` | 129 | 0 | 1 |
| `__init__.py::Match.evaluate_result` | 922 | 0 | 1 |
| `__init__.py::Parser.__repr__` | 453 | 0 | 3 |
| `__init__.py::Parser._regex_replace` | 613 | 0 | 1 |
| `__init__.py::Parser.fixed_fields` | 497 | 0 | 1 |
| `__init__.py::Parser.format` | 501 | 0 | 1 |
| `__init__.py::Parser.named_fields` | 493 | 0 | 1 |
| `__init__.py::Result.__contains__` | 907 | 0 | 1 |
| `__init__.py::Result.__getitem__` | 899 | 0 | 3 |
| `__init__.py::Result.__repr__` | 904 | 0 | 1 |
| `__init__.py::compile` | 1067 | 0 | 1 |
| `__init__.py::percentage` | 113 | 0 | 1 |
| `__init__.py::with_pattern` | 20 | 0 | 2 |
| `__init__.py::with_pattern.decorator` | 43 | 0 | 3 |
| `__init__.py::Parser.__init__` | 422 | 17 | 1 |
| `__init__.py::Parser._expand_named_fields` | 559 | 10 | 4 |
| `__init__.py::Parser._handle_field` | 657 | 125 | 30 |
| `__init__.py::Parser._match_re` | 473 | 5 | 6 |
| `__init__.py::Parser._search_re` | 459 | 4 | 4 |
| `__init__.py::Parser._to_group_name` | 634 | 6 | 8 |
| `__init__.py::Parser.evaluate_result` | 583 | 17 | 1 |
| `__init__.py::Parser.search` | 518 | 7 | 1 |
| `__init__.py::ResultIterator.__next__` | 943 | 6 | 1 |
| `__init__.py::date_convert` | 177 | 41 | 28 |
| `__init__.py::extract_format` | 358 | 29 | 9 |
| `__init__.py::int_convert.__call__` | 70 | 16 | 5 |
| `__init__.py::strf_date_convert` | 280 | 11 | 2 |
