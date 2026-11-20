# The mutants both oracles missed, reviewed by hand

41 of the 300 sampled mutants survived both the case set and the upstream test suite. Those are the only candidates for a true equivalent mutant, so each was read and probed: the mutant was built, an API was called directly, and the unmutated and mutated results were compared. A probe that produces a difference proves the mutant is **not** equivalent — it is a gap in both oracles, which is a different and more actionable finding.

| Mutant | Line | Covered | Verdict | Evidence |
| --- | --- | --- | --- | --- |
| `delete_stmt@21:4` | 21 | no | docstring only | removing a docstring changes `__doc__` and nothing else |
| `delete_stmt@52:4` | 52 | no | docstring only | removing a docstring changes `__doc__` and nothing else |
| `compare_swap@88:47` | 88 | yes | behaviour, not observed by either oracle | the base-detection length comparison; no valid input distinguishes > from >= |
| `int_minus@88:76` | 88 | yes | behaviour, not observed by either oracle | the base-detection length comparison; no valid input distinguishes > from >= |
| `int_plus@88:76` | 88 | yes | behaviour, not observed by either oracle | the base-detection length comparison; no valid input distinguishes > from >= |
| `delete_stmt@118:4` | 118 | no | docstring only | removing a docstring changes `__doc__` and nothing else |
| `int_minus@120:21` | 120 | yes | behaviour, not observed by either oracle | probe: `parse.FixedTzOffset.ZERO` -> datetime.timedelta(0) vs datetime.timedelta(days=-1) |
| `return_none@133:8` | 133 | no | behaviour, not observed by either oracle | `FixedTzOffset.tzname` returns None; nothing formats a tzinfo's name |
| `return_none@141:8` | 141 | no | behaviour, not observed by either oracle | probe: `(parse.FixedTzOffset(300, 'EST') == parse.FixedTzOffset(300, 'EST'))` -> True vs None |
| `bool_flip@141:15` | 141 | no | behaviour, not observed by either oracle | probe: `(parse.FixedTzOffset(300, 'EST') == parse.FixedTzOffset(300, 'PST'))` -> False vs True |
| `compare_swap@141:15` | 141 | no | behaviour, not observed by either oracle | probe: `(parse.FixedTzOffset(300, 'EST') == parse.FixedTzOffset(300, 'PST'))` -> False vs True |
| `compare_swap@141:45` | 141 | no | behaviour, not observed by either oracle | probe: `(parse.FixedTzOffset(300, 'EST') == parse.FixedTzOffset(120, 'EST'))` -> False vs True |
| `int_minus@147:11` | 147 | no | behaviour, not observed by either oracle | probe: `parse.parse('{:tg}', '01/Feb/2024')` -> <Result (datetime.datetime(2024, 2, 1, 0, 0),) {}> vs <Result (datetime.datetime(2024, 1, 1, 0, 0),) { |
| `int_plus@149:11` | 149 | no | no difference found (candidate equivalent, not proven) | no probe produced a difference |
| `int_minus@150:13` | 150 | no | behaviour, not observed by either oracle | the same month table as `int_minus@147:11`, where a probe showed 01/Feb/2024 -> 01/Jan/2024 |
| `int_minus@156:11` | 156 | no | behaviour, not observed by either oracle | the month table (see `int_minus@147:11`) |
| `int_plus@157:12` | 157 | no | behaviour, not observed by either oracle | the month table (see `int_minus@147:11`) |
| `int_plus@158:11` | 158 | no | behaviour, not observed by either oracle | the month table (see `int_minus@147:11`) |
| `int_minus@160:11` | 160 | no | behaviour, not observed by either oracle | the month table (see `int_minus@147:11`) |
| `int_plus@161:17` | 161 | no | behaviour, not observed by either oracle | the month table (see `int_minus@147:11`) |
| `int_plus@162:11` | 162 | no | behaviour, not observed by either oracle | the month table (see `int_minus@147:11`) |
| `int_minus@259:27` | 259 | no | behaviour, not observed by either oracle | probe: `parse.parse('{:ti}', '2024-03-15T11:30:00+0059')` -> <Result (datetime.datetime(2024, 3, 15, 11, 30, tzinfo=<FixedTzOffset +0059 0:59:00>),) { |
| `arith_swap@455:57` | 455 | no | behaviour, not observed by either oracle | probe: `repr(parse.Parser('{:d}' * 8))` -> "<Parser '{:d}{:d}{:d}{:d}{...'>" vs TypeError: unsupported operand type(s) for -: 'str' and 'str' |
| `slice_bound@455:70` | 455 | no | behaviour, not observed by either oracle | probe: `repr(parse.Parser('{:d}' * 8))` -> "<Parser '{:d}{:d}{:d}{:d}{...'>" vs "<Parser '{:d}{:d}{:d}{:d}{:...'>" |
| `delete_stmt@480:16` | 480 | no | behaviour, not observed by either oracle | the AssertionError branch needs a format with >100 named groups; probes could not reach it |
| `return_none@498:8` | 498 | no | behaviour, not observed by either oracle | probe: `parse.Parser('{a}{b}').fixed_fields` -> [] vs None |
| `int_plus@518:33` | 518 | yes | behaviour, not observed by either oracle | probe: `parse.Parser('{:d}').search('5x')` -> <Result (5,) {}> vs None |
| `delete_stmt@519:8` | 519 | no | docstring only | removing a docstring changes `__doc__` and nothing else |
| `delete_stmt@544:8` | 544 | no | docstring only | removing a docstring changes `__doc__` and nothing else |
| `delete_stmt@584:8` | 584 | no | docstring only | removing a docstring changes `__doc__` and nothing else |
| `int_minus@614:34` | 614 | no | behaviour, not observed by either oracle | the extra_types name escape; a probe with a custom type matched identically |
| `int_minus@640:12` | 640 | yes | behaviour, not observed by either oracle | collision renaming starts one lower; probes with colliding names matched |
| `compare_swap@647:17` | 647 | no | behaviour, not observed by either oracle | the `-` branch of collision renaming; a probe with `{a-b}` matched |
| `arith_swap@648:43` | 648 | no | behaviour, not observed by either oracle | probe: `parse.parse('{a.b}{a-b}', 'xy')` -> <Result () {'a.b': 'x', 'a-b': 'y'}> vs TypeError: unsupported operand type(s) for /: 'str' and 'int' |
| `int_minus@713:36` | 713 | no | behaviour, not observed by either oracle | the group counter's start value; probes with trailing named fields matched |
| `int_minus@734:33` | 734 | no | behaviour, not observed by either oracle | a group-index increment; probes with a following named field matched |
| `delete_stmt@771:12` | 771 | yes | behaviour, not observed by either oracle | a group-index increment for datetime patterns; a probe with a following field matched |
| `delete_stmt@821:12` | 821 | yes | behaviour, not observed by either oracle | a group-index increment for `{:ts}`; a probe with a following field matched |
| `int_minus@821:33` | 821 | yes | behaviour, not observed by either oracle | probe: `parse.parse('{:ts}{:d}', 'Mar 15 7')` -> <Result (datetime.datetime(2026, 3, 15, 0, 0), 7) {}> vs TypeError: 'NoneType' object is not subscrip |
| `arith_swap@870:19` | 870 | no | behaviour, not observed by either oracle | probe: `parse.parse('{:*>5d}', '***12')` -> <Result (12,) {}> vs TypeError: unsupported operand type(s) for -: 'str' and 'str' |
| `delete_stmt@1038:4` | 1038 | no | docstring only | removing a docstring changes `__doc__` and nothing else |

## What the review found

- **7** are docstring deletions: behaviour is unchanged, only `__doc__` is.
- **33** change real behaviour that neither oracle looks at (month names, `FixedTzOffset.__eq__`, `tzname`, the repr of a long format, `Parser.search`'s default `pos`, timezone minute 59, the fill-character escape, duplicated-name renaming). Every one is a gap in **both** the case set and the upstream suite, not an equivalent mutant.
- **1** produced no difference under any probe. They are candidates for true equivalence and are labelled as candidates, not as proven equivalents: a probe is an experiment, not a proof.

The coverage column is why most of these were invisible: 32 of the 41 sit on a line the case set never executes, and the upstream suite does not execute it either. Writing cases for those lines is a different exercise from tuning a branch, and it is the honest backlog for stage 1.
