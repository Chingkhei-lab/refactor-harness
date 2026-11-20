# Tuning the case set: what generalised and what was fit to the survivors

Evidence, not proof. The first pass is the headline; the tuned run on the same mutants is overfit by construction, because the new cases were written after reading the 65 covered survivors. The held-out run is the result.

| Run | Case set | Mutants | Kill rate | Covered mutants only |
| --- | --- | --- | --- | --- |
| First pass (headline) | 273 cases, `283155b0` | 300 (seed 12) | 139/300 = 46.3 % (95 % CI 41-52 %) | 138/203 = 68.0 % (95 % CI 61-74 %) |
| Tuned, same mutants (overfit) | 307 cases, `4dcdd838` | 300 | 180/300 = 60.0 % (95 % CI 54-65 %) | 163/203 = 80.3 % (95 % CI 74-85 %) |
| Held-out, original cases | 273 cases | 150 (seed 99, sites excluded: 300) | 58/150 = 38.7 % (95 % CI 31-47 %) | 56/86 = 65.1 % (95 % CI 55-74 %) |
| Held-out, tuned cases | 307 cases | 150 | 83/150 = 55.3 % (95 % CI 47-63 %) | 72/86 = 83.7 % (95 % CI 75-90 %) |

The held-out mutants are drawn from the 539 sites the first sample did not touch, stratified by operator and seeded (99), so no tuning case was written with those sites in view. The gain there is +16.7 percentage points, against +13.7 on the tuning set. The two intervals overlap, so the honest reading is "the new cases help, and the tuning set does not overstate by much" — not "tuning doubled the kill rate".

Newly killed on the tuning set: **41**; lost: **0**.

## What the 34 new cases were for

| Cluster | Cases | Survivor lines it targets |
| --- | --- | --- |
| integer sign and base (`{:d}` with `+`, `-`, `0x`, `0b`, `0o`; `{:x}/{ :b}/{ :o}`) | 12 | 74-93 |
| meridiem (`{:tt}` with `PM`/`am`, including the 12 o'clock cases) | 5 | 233-236 |
| no year in the input (`{:ts}`) | 2 | 284 |
| width, precision, alignment (`{:>10}`, `{:=10}`, `{:.3}`, `{:10.3}`, `{:5d}`) | 7 | 362-400, 842, 850 |
| repeated named field (`{n}{n}`) | 1 | 640 |
| two datetime fields in one format | 4 | 723-821 |
| timezone/month-name formats (`{:ti}`, `{:ta}`, `{:tc}`) | 3 | 723-821 |

`{:10.3}` is not a working format — the target raises `NotImplementedError` on it. It is kept because the exception is a deterministic observation and the width-with-precision branch is otherwise unreachable.

## What this does not say

- The held-out sample is another 150 of the same 839 sites on the same file; it tests generalisation across *sites*, not across targets.
- Upstream tests were not re-run on the held-out mutants, so the held-out table has no test-oracle column.
- A gain here is not a claim that the case set is complete: 96 of the first pass's survivors sit on lines no case executes, and writing cases for those lines is a different exercise from writing cases for a branch.
