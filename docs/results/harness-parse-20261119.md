# Harness baseline — parse 1.22.4

Evidence, not proof: this records what the target did under a fixed case set and a determinism shim. It says nothing about behaviour the cases never reach, and the coverage section below is the honest limit.

| Measurement | Value |
| --- | --- |
| Cases | 273 |
| Outcomes (return / exception / unsupported) | {'return': 261, 'exception': 12, 'unsupported': 0} |
| A/A passes | 5 |
| A/A noise (cases that varied) | **0** |
| Cases decided by the shim | 3 |
| Uncovered functions | 32 |
| Golden blobs | 273 |
| Controls applied | clock_frozen, random_seeded, uuid_frozen, hash_seed_0, tz_utc, locale_c, cwd_fixed, env_scrubbed |

## Cases by source

| Source | Cases |
| --- | --- |
| docs | 42 |
| grammar | 132 |
| grammar-random | 33 |
| hypothesis | 40 |
| malformed | 26 |

## A/A noise report

No case varied across passes.

## Cases the determinism shim decides

- `0167-parse-grammar`
- `0169-search-grammar`
- `0170-findall-grammar`

## Uncovered functions (the limit of these claims)

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
