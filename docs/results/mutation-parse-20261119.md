# Mutation testing - parse 1.22.4

Evidence, not proof: a kill rate says how often this case set noticed a seeded defect, not how many defects exist.

| Measurement | Value |
| --- | --- |
| Case set | 273 cases, sha256 `283155b0bbb98167...` |
| Observation schema | v2 |
| Sites enumerated | 839 |
| Mutants sampled | 300 (seed 12) |
| Sampling | stratified: min(3, available) per operator, rest proportional, one seed |
| Baseline pass | 0.98s |
| Est. per mutant | 2.98s |
| Oracle | one pass of the case set vs the unmutated baseline |
| Harness kill rate | **139/300 = 46.3%** |
| Upstream tests kill rate | **42/300 = 14.0%** |
| Union kill rate | **181/300 = 60.3%** |

## Sites by operator

| Operator | Sites | Sampled | Killed (harness) | Killed (tests) |
| --- | --- | --- | --- | --- |
| `compare_swap` | 93 | 40 | 27 | 4 |
| `arith_swap` | 77 | 23 | 11 | 3 |
| `int_plus` | 154 | 48 | 10 | 7 |
| `int_minus` | 154 | 56 | 12 | 13 |
| `bool_flip` | 24 | 11 | 7 | 1 |
| `return_none` | 49 | 19 | 10 | 3 |
| `delete_stmt` | 271 | 95 | 61 | 10 |
| `slice_bound` | 17 | 8 | 1 | 1 |

## Kill rates, all mutants vs covered mutants

| Oracle | All mutants | Only mutants on a line the case set executes |
| --- | --- | --- |
| Harness | 139/300 = 46.3 % | 138/203 = 68.0 % |
| Upstream test suite | 42/300 = 14.0 % | 10/203 = 4.9 % |
| Either (union) | 181/300 = 60.3 % | 148/203 = 72.9 % |

The covered-only column is the honest one: `97` of `300` mutants change a line the case set never reaches, so no observation could ever distinguish them. Both columns are reported because quoting only the covered one would hide how much of the file the cases miss.

## Survivors, three ways

| Group | Count | What it means |
| --- | --- | --- |
| Line never executed | 96 | a coverage statement, not a case-set weakness in the ordinary sense: the cases never reach the line |
| Covered, not observed | 63 | the cases execute the line but not the behaviour the mutation changes |
| Equivalent mutant (hand-confirmed) | 2 | no observable behaviour changed |

## Survivor table

| Mutant | Line | Covered | Upstream tests | Verdict | Why |
| --- | --- | --- | --- | --- | --- |
| `delete_stmt@21:4` | 21 | no | killed | not-covered | line never executed by the case set |
| `delete_stmt@46:8` | 46 | no | passed | not-covered | line never executed by the case set |
| `return_none@46:8` | 46 | no | passed | not-covered | line never executed by the case set |
| `delete_stmt@52:4` | 52 | no | killed | not-covered | line never executed by the case set |
| `int_plus@73:27` | 73 | yes | passed | covered-not-observed | int_convert's sign/base detection: the public cases have no 0x/0b/0o or signed-prefixed input, so the changed  |
| `compare_swap@74:13` | 74 | yes | passed | covered-not-observed | int_convert's sign/base detection: the public cases have no 0x/0b/0o or signed-prefixed input, so the changed  |
| `int_plus@74:20` | 74 | yes | passed | covered-not-observed | int_convert's sign/base detection: the public cases have no 0x/0b/0o or signed-prefixed input, so the changed  |
| `int_minus@75:19` | 75 | no | passed | not-covered | line never executed by the case set |
| `int_plus@76:27` | 76 | no | passed | not-covered | line never executed by the case set |
| `int_minus@79:27` | 79 | yes | passed | covered-not-observed | int_convert's sign/base detection: the public cases have no 0x/0b/0o or signed-prefixed input, so the changed  |
| `compare_swap@88:47` | 88 | yes | killed | covered-not-observed | int_convert's sign/base detection: the public cases have no 0x/0b/0o or signed-prefixed input, so the changed  |
| `int_minus@88:76` | 88 | yes | killed | covered-not-observed | int_convert's sign/base detection: the public cases have no 0x/0b/0o or signed-prefixed input, so the changed  |
| `int_plus@88:76` | 88 | yes | killed | covered-not-observed | int_convert's sign/base detection: the public cases have no 0x/0b/0o or signed-prefixed input, so the changed  |
| `int_plus@89:41` | 89 | yes | passed | covered-not-observed | int_convert's sign/base detection: the public cases have no 0x/0b/0o or signed-prefixed input, so the changed  |
| `int_minus@90:27` | 90 | no | passed | not-covered | line never executed by the case set |
| `int_minus@91:43` | 91 | yes | passed | covered-not-observed | int_convert's sign/base detection: the public cases have no 0x/0b/0o or signed-prefixed input, so the changed  |
| `int_minus@92:27` | 92 | no | passed | not-covered | line never executed by the case set |
| `int_plus@92:27` | 92 | no | passed | not-covered | line never executed by the case set |
| `int_minus@93:43` | 93 | yes | passed | covered-not-observed | int_convert's sign/base detection: the public cases have no 0x/0b/0o or signed-prefixed input, so the changed  |
| `int_plus@93:43` | 93 | yes | passed | covered-not-observed | int_convert's sign/base detection: the public cases have no 0x/0b/0o or signed-prefixed input, so the changed  |
| `int_plus@94:27` | 94 | no | passed | not-covered | line never executed by the case set |
| `delete_stmt@118:4` | 118 | no | killed | not-covered | line never executed by the case set |
| `int_minus@120:21` | 120 | yes | killed | covered-not-observed | FixedTzOffset.ZERO is never read: the case set has no %z format |
| `delete_stmt@124:8` | 124 | no | passed | not-covered | line never executed by the case set |
| `return_none@127:8` | 127 | no | passed | not-covered | line never executed by the case set |
| `return_none@133:8` | 133 | no | killed | not-covered | line never executed by the case set |
| `return_none@141:8` | 141 | no | killed | not-covered | line never executed by the case set |
| `bool_flip@141:15` | 141 | no | killed | not-covered | line never executed by the case set |
| `compare_swap@141:15` | 141 | no | killed | not-covered | line never executed by the case set |
| `compare_swap@141:45` | 141 | no | killed | not-covered | line never executed by the case set |
| `int_minus@147:11` | 147 | no | killed | not-covered | line never executed by the case set |
| `int_plus@149:11` | 149 | no | killed | not-covered | line never executed by the case set |
| `int_minus@150:13` | 150 | no | killed | not-covered | line never executed by the case set |
| `int_minus@156:11` | 156 | no | killed | not-covered | line never executed by the case set |
| `int_plus@157:12` | 157 | no | killed | not-covered | line never executed by the case set |
| `int_plus@158:11` | 158 | no | killed | not-covered | line never executed by the case set |
| `int_minus@160:11` | 160 | no | killed | not-covered | line never executed by the case set |
| `int_plus@161:17` | 161 | no | killed | not-covered | line never executed by the case set |
| `int_plus@162:11` | 162 | no | killed | not-covered | line never executed by the case set |
| `int_plus@165:16` | 165 | no | passed | not-covered | line never executed by the case set |
| `delete_stmt@221:16` | 221 | no | passed | not-covered | line never executed by the case set |
| `int_minus@222:32` | 222 | no | passed | not-covered | line never executed by the case set |
| `bool_flip@233:11` | 233 | yes | passed | covered-not-observed | date_convert AM/PM handling: no case uses %p |
| `bool_flip@236:13` | 236 | yes | passed | covered-not-observed | date_convert AM/PM handling: no case uses %p |
| `compare_swap@236:28` | 236 | yes | passed | covered-not-observed | date_convert AM/PM handling: no case uses %p |
| `int_minus@240:17` | 240 | no | passed | not-covered | line never executed by the case set |
| `int_plus@240:17` | 240 | no | passed | not-covered | line never executed by the case set |
| `int_minus@245:27` | 245 | no | passed | not-covered | line never executed by the case set |
| `int_plus@245:27` | 245 | no | passed | not-covered | line never executed by the case set |
| `delete_stmt@252:12` | 252 | no | passed | not-covered | line never executed by the case set |
| `compare_swap@253:15` | 253 | no | passed | not-covered | line never executed by the case set |
| `int_plus@254:30` | 254 | no | passed | not-covered | line never executed by the case set |
| `slice_bound@254:30` | 254 | no | passed | not-covered | line never executed by the case set |
| `compare_swap@255:17` | 255 | no | passed | not-covered | line never executed by the case set |
| `int_plus@255:28` | 255 | no | passed | not-covered | line never executed by the case set |
| `int_minus@256:30` | 256 | no | passed | not-covered | line never executed by the case set |
| `int_plus@258:30` | 258 | no | passed | not-covered | line never executed by the case set |
| `compare_swap@259:15` | 259 | no | passed | not-covered | line never executed by the case set |
| `int_minus@259:27` | 259 | no | killed | not-covered | line never executed by the case set |
| `delete_stmt@261:12` | 261 | no | passed | not-covered | line never executed by the case set |
| `bool_flip@284:7` | 284 | yes | passed | covered-not-observed | the missing-year default: no case uses a date format without %y/%Y |
| `delete_stmt@362:8` | 362 | yes | passed | covered-not-observed | extract_format width/precision branches: the case formats do not take those paths |
| `int_minus@362:23` | 362 | yes | passed | covered-not-observed | extract_format width/precision branches: the case formats do not take those paths |
| `delete_stmt@365:8` | 365 | no | passed | not-covered | line never executed by the case set |
| `int_minus@365:22` | 365 | no | passed | not-covered | line never executed by the case set |
| `int_plus@366:23` | 366 | no | passed | not-covered | line never executed by the case set |
| `int_minus@367:24` | 367 | no | passed | not-covered | line never executed by the case set |
| `slice_bound@367:24` | 367 | no | passed | not-covered | line never executed by the case set |
| `int_minus@381:24` | 381 | yes | passed | covered-not-observed | extract_format width/precision branches: the case formats do not take those paths |
| `int_minus@382:24` | 382 | yes | killed | covered-not-observed | extract_format width/precision branches: the case formats do not take those paths |
| `slice_bound@382:24` | 382 | yes | passed | covered-not-observed | extract_format width/precision branches: the case formats do not take those paths |
| `int_plus@386:24` | 386 | no | passed | not-covered | line never executed by the case set |
| `slice_bound@386:24` | 386 | no | passed | not-covered | line never executed by the case set |
| `slice_bound@389:24` | 389 | no | passed | not-covered | line never executed by the case set |
| `int_plus@395:24` | 395 | yes | passed | covered-not-observed | extract_format width/precision branches: the case formats do not take those paths |
| `slice_bound@395:24` | 395 | yes | passed | covered-not-observed | extract_format width/precision branches: the case formats do not take those paths |
| `int_minus@400:32` | 400 | yes | passed | covered-not-observed | extract_format width/precision branches: the case formats do not take those paths |
| `int_plus@400:32` | 400 | yes | passed | covered-not-observed | extract_format width/precision branches: the case formats do not take those paths |
| `arith_swap@455:57` | 455 | no | killed | not-covered | line never executed by the case set |
| `slice_bound@455:70` | 455 | no | killed | not-covered | line never executed by the case set |
| `delete_stmt@480:16` | 480 | no | killed | not-covered | line never executed by the case set |
| `return_none@494:8` | 494 | no | passed | not-covered | line never executed by the case set |
| `return_none@498:8` | 498 | no | killed | not-covered | line never executed by the case set |
| `int_plus@518:33` | 518 | yes | killed | equivalent | the public search() always passes pos explicitly, so the default is dead |
| `delete_stmt@519:8` | 519 | no | killed | not-covered | line never executed by the case set |
| `delete_stmt@544:8` | 544 | no | killed | not-covered | line never executed by the case set |
| `compare_swap@553:11` | 553 | yes | passed | equivalent | the public findall() always passes endpos explicitly, so the None check is dead through the public API |
| `delete_stmt@575:20` | 575 | no | passed | not-covered | line never executed by the case set |
| `int_minus@576:31` | 576 | no | passed | not-covered | line never executed by the case set |
| `int_minus@576:34` | 576 | no | passed | not-covered | line never executed by the case set |
| `int_plus@576:34` | 576 | no | passed | not-covered | line never executed by the case set |
| `delete_stmt@584:8` | 584 | no | killed | not-covered | line never executed by the case set |
| `int_minus@614:34` | 614 | no | killed | not-covered | line never executed by the case set |
| `int_minus@640:12` | 640 | yes | killed | covered-not-observed | repeated named fields: no case repeats a field name |
| `int_plus@640:12` | 640 | yes | passed | covered-not-observed | repeated named fields: no case repeats a field name |
| `arith_swap@644:43` | 644 | no | passed | not-covered | line never executed by the case set |
| `compare_swap@645:17` | 645 | no | passed | not-covered | line never executed by the case set |
| `compare_swap@647:17` | 647 | no | killed | not-covered | line never executed by the case set |
| `arith_swap@648:43` | 648 | no | killed | not-covered | line never executed by the case set |
| `arith_swap@678:24` | 678 | no | passed | not-covered | line never executed by the case set |
| `delete_stmt@684:16` | 684 | no | passed | not-covered | line never executed by the case set |
| `delete_stmt@710:12` | 710 | no | passed | not-covered | line never executed by the case set |
| `int_minus@711:77` | 711 | no | passed | not-covered | line never executed by the case set |
| `int_plus@711:77` | 711 | no | passed | not-covered | line never executed by the case set |
| `int_minus@713:36` | 713 | no | killed | not-covered | line never executed by the case set |
| `int_plus@713:36` | 713 | no | passed | not-covered | line never executed by the case set |
| `int_minus@723:33` | 723 | yes | passed | covered-not-observed | _handle_field group numbering and datetime converters: no case puts two datetime fields, a month name or a tim |
| `int_plus@723:33` | 723 | yes | passed | covered-not-observed | _handle_field group numbering and datetime converters: no case puts two datetime fields, a month name or a tim |
| `int_minus@727:33` | 727 | yes | passed | covered-not-observed | _handle_field group numbering and datetime converters: no case puts two datetime fields, a month name or a tim |
| `int_plus@727:33` | 727 | yes | passed | covered-not-observed | _handle_field group numbering and datetime converters: no case puts two datetime fields, a month name or a tim |
| `int_minus@731:33` | 731 | yes | passed | covered-not-observed | _handle_field group numbering and datetime converters: no case puts two datetime fields, a month name or a tim |
| `int_minus@734:33` | 734 | no | killed | not-covered | line never executed by the case set |
| `delete_stmt@748:12` | 748 | no | passed | not-covered | line never executed by the case set |
| `delete_stmt@753:12` | 753 | no | passed | not-covered | line never executed by the case set |
| `arith_swap@756:24` | 756 | no | passed | not-covered | line never executed by the case set |
| `delete_stmt@771:12` | 771 | yes | killed | covered-not-observed | _handle_field group numbering and datetime converters: no case puts two datetime fields, a month name or a tim |
| `int_minus@775:67` | 775 | yes | passed | covered-not-observed | _handle_field group numbering and datetime converters: no case puts two datetime fields, a month name or a tim |
| `int_plus@775:67` | 775 | yes | passed | covered-not-observed | _handle_field group numbering and datetime converters: no case puts two datetime fields, a month name or a tim |
| `int_plus@776:33` | 776 | yes | passed | covered-not-observed | _handle_field group numbering and datetime converters: no case puts two datetime fields, a month name or a tim |
| `arith_swap@782:34` | 782 | no | passed | not-covered | line never executed by the case set |
| `int_plus@782:38` | 782 | no | passed | not-covered | line never executed by the case set |
| `int_minus@782:59` | 782 | no | passed | not-covered | line never executed by the case set |
| `int_minus@782:69` | 782 | no | passed | not-covered | line never executed by the case set |
| `delete_stmt@787:12` | 787 | yes | passed | covered-not-observed | _handle_field group numbering and datetime converters: no case puts two datetime fields, a month name or a tim |
| `int_plus@790:38` | 790 | no | passed | not-covered | line never executed by the case set |
| `arith_swap@790:65` | 790 | no | passed | not-covered | line never executed by the case set |
| `delete_stmt@792:12` | 792 | yes | passed | covered-not-observed | _handle_field group numbering and datetime converters: no case puts two datetime fields, a month name or a tim |
| `int_minus@792:33` | 792 | yes | passed | covered-not-observed | _handle_field group numbering and datetime converters: no case puts two datetime fields, a month name or a tim |
| `int_plus@792:33` | 792 | yes | passed | covered-not-observed | _handle_field group numbering and datetime converters: no case puts two datetime fields, a month name or a tim |
| `arith_swap@798:52` | 798 | yes | passed | covered-not-observed | _handle_field group numbering and datetime converters: no case puts two datetime fields, a month name or a tim |
| `int_minus@798:56` | 798 | yes | passed | covered-not-observed | _handle_field group numbering and datetime converters: no case puts two datetime fields, a month name or a tim |
| `int_plus@798:56` | 798 | yes | passed | covered-not-observed | _handle_field group numbering and datetime converters: no case puts two datetime fields, a month name or a tim |
| `int_minus@798:77` | 798 | yes | passed | covered-not-observed | _handle_field group numbering and datetime converters: no case puts two datetime fields, a month name or a tim |
| `int_plus@798:77` | 798 | yes | passed | covered-not-observed | _handle_field group numbering and datetime converters: no case puts two datetime fields, a month name or a tim |
| `delete_stmt@799:12` | 799 | yes | passed | covered-not-observed | _handle_field group numbering and datetime converters: no case puts two datetime fields, a month name or a tim |
| `delete_stmt@804:12` | 804 | yes | passed | covered-not-observed | _handle_field group numbering and datetime converters: no case puts two datetime fields, a month name or a tim |
| `delete_stmt@805:12` | 805 | yes | passed | covered-not-observed | _handle_field group numbering and datetime converters: no case puts two datetime fields, a month name or a tim |
| `int_minus@805:33` | 805 | yes | passed | covered-not-observed | _handle_field group numbering and datetime converters: no case puts two datetime fields, a month name or a tim |
| `delete_stmt@811:12` | 811 | yes | passed | covered-not-observed | _handle_field group numbering and datetime converters: no case puts two datetime fields, a month name or a tim |
| `int_plus@811:33` | 811 | yes | passed | covered-not-observed | _handle_field group numbering and datetime converters: no case puts two datetime fields, a month name or a tim |
| `int_minus@816:33` | 816 | yes | passed | covered-not-observed | _handle_field group numbering and datetime converters: no case puts two datetime fields, a month name or a tim |
| `delete_stmt@820:12` | 820 | yes | passed | covered-not-observed | _handle_field group numbering and datetime converters: no case puts two datetime fields, a month name or a tim |
| `arith_swap@820:51` | 820 | yes | passed | covered-not-observed | _handle_field group numbering and datetime converters: no case puts two datetime fields, a month name or a tim |
| `int_minus@820:65` | 820 | yes | passed | covered-not-observed | _handle_field group numbering and datetime converters: no case puts two datetime fields, a month name or a tim |
| `int_minus@820:76` | 820 | yes | passed | covered-not-observed | _handle_field group numbering and datetime converters: no case puts two datetime fields, a month name or a tim |
| `delete_stmt@821:12` | 821 | yes | killed | covered-not-observed | _handle_field group numbering and datetime converters: no case puts two datetime fields, a month name or a tim |
| `int_minus@821:33` | 821 | yes | killed | covered-not-observed | _handle_field group numbering and datetime converters: no case puts two datetime fields, a month name or a tim |
| `arith_swap@830:20` | 830 | no | passed | not-covered | line never executed by the case set |
| `compare_swap@842:15` | 842 | yes | passed | covered-not-observed | an '=' alignment format is not in the case set |
| `delete_stmt@847:16` | 847 | no | passed | not-covered | line never executed by the case set |
| `delete_stmt@850:12` | 850 | yes | passed | covered-not-observed | a signed fixed field is not in the case set |
| `arith_swap@870:19` | 870 | no | killed | not-covered | line never executed by the case set |
| `arith_swap@878:16` | 878 | no | passed | not-covered | line never executed by the case set |
| `return_none@901:12` | 901 | no | passed | not-covered | line never executed by the case set |
| `delete_stmt@902:8` | 902 | no | passed | not-covered | line never executed by the case set |
| `compare_swap@908:15` | 908 | no | passed | not-covered | line never executed by the case set |
| `delete_stmt@937:8` | 937 | yes | passed | covered-not-observed | Result/ResultIterator attributes that only matter for explicit endpos or re-iteration |
| `delete_stmt@938:8` | 938 | yes | passed | covered-not-observed | Result/ResultIterator attributes that only matter for explicit endpos or re-iteration |
| `return_none@941:8` | 941 | yes | passed | covered-not-observed | Result/ResultIterator attributes that only matter for explicit endpos or re-iteration |
| `delete_stmt@1038:4` | 1038 | no | killed | not-covered | line never executed by the case set |
| `return_none@1083:4` | 1083 | no | passed | not-covered | line never executed by the case set |

Verdicts live in `docs/results/mutation-verdicts.json` and the coverage split in `docs/results/mutation-survivors.json`, so every survivor has a stated reason rather than a number.

