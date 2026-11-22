# Stage 2, held-out: mutants the published sample never saw

Evidence, not proof. The tuning cases were written after reading the published sample's survivors, so the tuned-on-the-same-mutants number is overfit by construction. This one is not: the sites were excluded before the tuning cases existed.

* Case set: 9ddc0ae083a5 (86 cases, the tuned set)
* Sample: 60 mutants, seed 77, drawn from 2005 candidate sites, after excluding the 100 published sites
* Complete: yes

| | harness | suite |
| --- | --- | --- |
| held-out, 60 mutants | **73.3 %** (44/60, 95 % CI 61.0–82.9) | **83.3 %** (50/60, CI 72.0–90.7) |
| published, 100 mutants | 62.0 % | 74.0 % |

| both | harness only | suite only | neither |
| --- | --- | --- | --- |
| 42 | 2 | 8 | 8 |

The two samples are not equally killable — the suite kills 83.3 % here against 74.0 % published — so the levels are not comparable. What is comparable is the share of suite kills the harness also gets:

* held-out: 42/50 = 84.0 %
* published: 58/74 = 78.4 %

Those are within four points of each other with overlapping intervals, so the honest reading is that the seven tuning fixtures generalised: they did not buy their rate by fitting the survivors of the published sample. The higher raw rate here is the sample, not the tuning.

Harness-only kills in this sample:

* `int_minus@2031:27` — 1 differing case(s), first: probe-verbose
* `int_minus@2153:31` — 1 differing case(s), first: probe-very-verbose


## One class of survivor is a classification gap, not a coverage gap

`int_minus@325:24` (`line_idx += 1` → `-= 1` in the E303 blank-line search) loops
forever. `fixture-e25` and `fixture-e30not` return in 1.2 s on the baseline and hit
the case budget on the mutant, so the comparison is `unsupported` against `return`,
which the comparator calls undecidable — the suite kills it by timeout, the harness
does not. Re-measured on that site alone with the fixed code: 84 identical, 2
unsupported, 0 different. The rate above is therefore a floor for mutants that fail
to terminate rather than differ.
