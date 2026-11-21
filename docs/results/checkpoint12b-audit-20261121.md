# Stage-2 mutation result: audit and the three defects it hid

The review said 100 of 100 kills by both oracles was too clean, and to check the
evaluator before believing it. It was too clean. Checking it found **three**
defects, and the first one is the reason the score was perfect:

1. **The mutator re-printed the whole module.** `apply_site` returned
   `ast.unparse(tree)`, so every mutant — every single one — rewrote all 2 717
   lines of `pycodestyle.py` into 2 095. Line numbers moved, echoed source lines
   changed, and two of the 77 cases print both. 77 of the 100 mutants differed on
   the first of those cases and 75 on the second; **38 differed on nothing else**.
2. **The CLI observation contained the target's own source.** Even with a minimal
   edit, a traceback names the file, the line and the statement, so those same two
   cases turn any staged tree, any two-line patch and any line-shifting edit into
   a difference. This is the defect that made the *migration* report two approvals
   that differed only by line numbers.
3. **Nothing observed an API nobody calls.** The one difference the upstream suite
   saw and the harness did not — `get_parser().add_option` — was invisible to
   every case, by construction.

A fourth thing fell out of the audit and is reported separately in §6: the
stage-1 case set was not reproducible. Rebuilding it produced three different
hashes.

| check the review asked for | verdict |
| --- | --- |
| 1. no-op canary must survive both oracles | **now passes** (`comment`, `whitespace`, `reformat` → 0 differences); before the fix a re-print of the module was killed by both |
| 2. why was each mutant killed | 77/100 on one case, 75/100 on a second, 38 on exactly that pair — the two cases that print the target's source |
| 3. mutants on lines no case executes | **15 of 100**, and all 15 were reported killed |
| 4. ten hand-reviewed mutants with evidence | **§8**: four harness-only kills, one broad break, two suite-only, three survivors — `docs/results/mutant-evidence-pycodestyle-20261121.json` |
| 5. timing | §7: the plan's 24.7 s was the pass, not the mutant |
| quote the result as "under review" | it was; the re-run is §8 |

## 1. The mutator rewrote the file (`apply_site`)

The old mutator changed exactly one node in the AST and then serialised the whole
tree. Every property the harness checked still held — the mutant parsed, one
operator had changed, the site was addressable — which is why the canary test at
checkpoint 12a did not catch it: that canary *constructed* its comment-only mutant
by editing the text, so it never went through `apply_site`.

The measurement that makes it visible, on the same module the report cites:

```
pycodestyle.py, baseline                 2717 lines
pycodestyle.py, ast.unparse(parse(src))  2095 lines   (every mutant, every time)
```

What that did to the score, from the published run
(`docs/results/mutation-pycodestyle-20261120.json`):

| | mutants | note |
| --- | --- | --- |
| differed on `probe-config-int-error` | 77 / 100 | prints a traceback: file, line, source line |
| differed on `probe-deprecated-section` | 75 / 100 | prints a `UserWarning` with `file:line` |
| differed on **exactly** that pair | 38 / 100 | the "kill" is the re-print |
| differed on ≥ 39 of 77 cases | 12 / 100 | "the tool is broken", not "the behaviour changed" |
| on a line no case executes, and killed | 15 / 100 | impossible unless the difference is the file layout |

`apply_site` now splices the original bytes and never re-prints the tree. It is
byte-exact on purpose: `ast` column offsets are UTF-8 *byte* offsets, so the edit
is done on encoded lines. `mutate()` then enforces the invariant that the old
version could not even state — **one contiguous changed region, on the site's own
line** — and refuses the result otherwise. All 2 108 pycodestyle sites and all 839
`parse` sites apply under that rule.

Two consequences, both corrections:

* a chained `a < b < c` or `a and b and c` is one site and now flips **one**
  token, so the mutant is a one-token change;
* `delete_stmt` uses `pass` for a statement alone in its block, and `...` for one
  that shares its line with an `if` header, instead of producing a file that does
  not parse.

## 2. The observation contained the target's source

With minimal edits, `probe-config-int-error` still differed, and the diff was the
tree's path:

```
-  File "...\.harness\verify-trees\old\pycodestyle.py", line 2717, in <module>
+  File "...\.harness\kill-probe\mutant\pycodestyle.py", line 2717, in <module>
```

Same class as the staged-path bug already fixed in `verify_change` (two trees,
one path), one level down. Left in, it is three false positives at once: a tree
staged anywhere differs, a patch that adds two lines differs, and every mutant
differs — which is exactly what "a perfect case set" would also look like.

`harness.projections.canonicalise_stream` keeps the exception type, its message
and the frame names, and drops the path, the line number, the echoed source line
and its caret line. It folds `\r\n` to `\n` because that is the host's text
translation, not the program's output — the same target prints `\r\n` on Windows
and `\n` on Linux through a pipe, so a raw stream cannot be compared across
platforms. It is named `cli_stream_projected` in every CLI observation's
`controls`, and the observation schema went to 3 because the values changed.

Effect on the migration report, measured: `probe-config-int-error` and
`probe-deprecated-section` are now **identical**, and the two approvals that
existed only because a line number moved are gone. The rest of the approvals
(parser text) are unchanged; their digests were re-derived because the projection
also normalises CRLF.

## 3. An API nobody calls is invisible to a program (`kind: "api"`)

The suite-only finding was `get_parser().add_option`. A CLI case cannot reach it;
a call case returns the object without describing it. Case kind `api` imports the
module and snapshots the surface — public names, `inspect.signature` of every
callable, class bases and attributes, stable scalar values, the first line of the
module docstring — plus, when the case names a call, the type, bases and
attributes of what that call returns. Values whose `repr` carries an address are
reduced to their type name, so the snapshot is deterministic (A/A verified).

On the migration it reports the difference the suite found, as an approved
difference with a witness:

```
api-module   $.value.classes.OptionParser      removed (OptionParser), added (argparse)
api-parser   $.value.call.attributes           type OptionParser -> ArgumentParser
             attributes removed: add_option, add_option_group, add_options,
                                 allow_interspersed_args, check_values, defaults, ...
             attributes added:   add_argument, add_argument_group, add_help, ...
```

The case set is now **79 cases** (42 fixture excerpts, 35 option probes, 2 api
snapshots), hash `8f04551c95d70c5f7618c6388df5de90d995d7534549e52588394ff7f9109dc9`.
`verify-change` on 2 rounds × 79: **142 identical, 16 different (8 approved), 0
unapproved, 0 stale, 0 unsupported, 0 flaky**.

## 4. The canaries

`scripts/mutation_canary.py` runs four mutants that change no *behaviour* through
the whole pipeline, both oracles, and is now a CI step:

| canary | edit | harness | suite | expected |
| --- | --- | --- | --- | --- |
| `comment` | a comment line appended | **0 differences** | 763 passed, 12 skipped | survives |
| `whitespace` | spaces around `=` on line 65 | **0 differences** | `test_own_dog_food` fails | survives behaviourally |
| `reformat` | `ast.unparse(parse(src))` — the old mutator | **0 differences** | `test_own_dog_food` fails | survives behaviourally |
| `docstring` | one word of the module docstring | 2 differences, `doc_first_line`, both api cases | 763 passed, 12 skipped | seen by the api cases |

The `reformat` canary is the one that would have caught defect 1: it is
behaviour-preserving and it rewrites every line. The first version of the
evaluator killed it on **79 of 79** cases.

`test_own_dog_food` is worth naming: it runs `pycodestyle` over its own source, so
the suite fails on a formatting change. A mutant killed by that test and nothing
else is killed **cosmetically**, and the audit counts those separately rather than
as the suite noticing a behaviour.

## 5. Ten mutants by hand

`scripts/mutant_evidence.py` prints, for one site: the exact changed lines (the
region, not a line-by-line zip — deleting one line shifts every line after it, and
a naive diff calls a one-line deletion a 2 572-line edit), the cases that differ
with a unified diff of the first differing field, and the suite's failing test ids.
The ten rows are tabulated in §8; the full evidence is
`docs/results/mutant-evidence-pycodestyle-20261121.json`.

## 6. The stage-1 case set was not reproducible

Found while re-running stage 1. `harness_baseline.py` rebuilds
`harness/cases/parse.json` and used to overwrite it. Rebuilding produced three
different case-set hashes from the same code and library version:

```
283155b0bbb98167   committed, and cited by every stage-1 number in v0.3
ed283159d51e35b3   produced by a baseline run on 2026-11-21
49e73e45892a6230   produced by a fresh call to build_parse_cases() minutes later
```

The difference is one of the 40 `hypothesis` inputs (`{:d}` against `2.5` became
`{:d}` against `123`) — `derandomize=True` did not make the collection reproducible
here. A case set whose identity changes when it is rebuilt cannot be the thing a
report's hash refers to, so:

* the 40 inputs are frozen as data in `harness/cases.py` with that reason;
* `harness_baseline.py` now **verifies** the committed file and refuses to
  overwrite it unless `--write-cases` is passed;
* `tests/test_harness_cases.py` asserts that regeneration reproduces the committed
  hash, and that two regenerations in one process agree.

The stage-1 numbers in this document are on the `ed283159…` set (273 cases, same
counts by source: 42 docs, 132 grammar, 33 grammar-random, 40 generated, 26
malformed). The `283155b0…` numbers in v0.3 are superseded, and the difference
between the two runs is reported rather than smoothed over.

## 7. Timing: the plan's number was the pass, not the mutant

`plan.json` said `baseline_seconds: 24.69` and `estimated_seconds_per_mutant:
26.69`, and the review multiplied 100 × 24.7 = 41 min against a reported 54. Both
numbers are right and they measure different things: the estimate added a flat
overhead to the *harness pass*, while a mutant also pays for its tree copy and the
upstream suite.

Measured, from the published run's own `results.json` (sum of per-mutant
`seconds`): **3 237.6 s = 32.4 s per mutant = 54 min**. The decomposition measured
serially on this host:

| phase | seconds |
| --- | --- |
| harness pass, 79 CLI cases | **29.1** |
| tree copy + mutant write | **0.5** |
| upstream suite, `-x`, healthy mutant | 9.3 (7.3 of it session overhead) |
| upstream suite, full run, on a mutant that breaks 737 tests | **84.5** |

So a mutant costs about **32 s**: 29 for the case set, 0.5 for the copy, and the
suite on top of that — small for a mutant that fails fast, large for one that
breaks the world, because the full run executes all 775 tests. The published run's
own `results.json` sums to 3 237.6 s for 100 mutants = 32.4 s, so the 54 minutes
is the right number and the 41-minute estimate was 24.7 s × 100 on the pass alone.

## 8. The re-run

Both runs were repeated end to end with the fixed mutator, the projection and the
extended case set. Seeds are unchanged (`parse` 12, `pycodestyle` 21), so the same
sites were sampled and the two runs are directly comparable.

### Stage 2 (`pycodestyle`, 79 cases, hash `8f04551c…`, 100 mutants, seed 21)

| | both | harness only | suite only | neither |
| --- | --- | --- | --- | --- |
| **after the fix** | **58** | **4** | **16** | **22** |
| *(published, defective)* | *100* | *0* | *0* | *0* |

* harness **62/100 = 62.0 %** (95 % CI 52.2–70.9), suite **74/100 = 74.0 %**
  (64.6–81.6), harness-only **4** (1.6–9.8), 22 mutants neither oracle notices.
* **no kill on a line no case executes** — the `killed-on-a-line-nothing-runs`
  label is empty, where the published run had 15.
* the top differing case appears 24 times, not 77: the artefact is gone and the
  distribution is over fixtures, which is what a case set noticing behaviour looks
  like.
* 11 kills differ on at least half the cases — those are mutants that break the
  tool broadly (deleting `PROJECT_CONFIG`, emptying a check), and the suite kills
  them too. 51 differ on one to thirteen cases.

| operator | mutants | harness | suite | neither |
| --- | --- | --- | --- | --- |
| arith_swap | 5 | 3 | 3 | 1 |
| bool_flip | 12 | 8 | 10 | 2 |
| compare_swap | 20 | 13 | 17 | 3 |
| delete_stmt | 18 | 16 | 17 | 1 |
| int_minus | 17 | 7 | 12 | 5 |
| int_plus | 18 | 9 | 7 | 8 |
| return_none | 5 | 4 | 4 | 0 |
| slice_bound | 5 | 4 | 4 | 0 |

`return_none` and `slice_bound` are the small families; `int_plus` is where the
case set is weakest, because a constant in a check's threshold is often not
reached by a 40-line fixture excerpt.

### Ten mutants by hand

Every row: the edit, the cases that differ, the witness, and what the suite said.
Full text in `docs/results/mutant-evidence-pycodestyle-20261121.json`.

| site | edit | differ | witness | suite |
| --- | --- | --- | --- | --- |
| `int_plus@334:24` | `next_idx += 1` → `+= 2` | 1 (`fixture-w29`) | the E302 report for line 6 disappears — the fixture has two adjacent definitions and the counter now skips one | passed |
| `int_plus@1317:33` | `backslash = (end[0], …)` → `(end[1], …)` | 1 (`fixture-e50`) | four `E502 the backslash is redundant` reports appear that should not | passed |
| `int_plus@2227:39` | `text[5:]` → `text[6:]` | 1 (`probe-statistics`) | statistics prints `E225 issing whitespace around operator` — the message lost its first character | passed |
| `arith_swap@2320:22` | `'    ' + doc` → `'    ' - doc` | 1 (`probe-show-source-and-pep8`) | `TypeError: unsupported operand type(s) for -: 'str' and 'str'`, observed as a traceback projected to type, message and frames | passed |
| `delete_stmt@80:0` | delete `PROJECT_CONFIG = ('setup.cfg', 'tox.ini')` | 73 | config discovery stops working everywhere: nearly every fixture case changes | killed |
| `bool_flip@181:11` | `args and args[0] in …` → `or` | 0 | none — `register_check`'s validation is an API contract no case exercises | **killed** (`test_register_invalid_check`) |
| `compare_swap@186:11` | `_get_parameters(...) == ['self', 'tree']` → `!=` | 0 | none — no case registers an AST check | **killed** (3 failures, `test_check_nullbytes` and others) |
| `bool_flip@74:12` | config home `or` → `and` | 0 | none — the line is not executed by any case | passed |
| `int_plus@406:55` | `ancestor_level == 0` → `== 1` | 0 | none — no fixture has the blank-line pattern this decides on | passed |
| `delete_stmt@173:4` | delete a docstring statement | 0 | none — deleting a function's docstring is a public-surface change the snapshot does not reach (it records the module's docstring, not each function's) | passed |

The four harness-only kills are the interesting ones: each is **one case**, a
different case, and each witness is a specific behavioural difference in output
that the suite's assertions do not check. The two suite-only kills are both
`register_check` contracts — behaviour reachable only through the API, which is a
case-set gap rather than an evaluator defect, and it is the same gap that made the
API snapshot necessary in the first place.

### Stage 1 (`parse`, 273 cases, hash `ed283159…`, 300 mutants, seed 12)

The stage-1 numbers were recomputed on the reproducible case set (§6) with the
fixed mutator:

| | both | harness only | suite only | neither |
| --- | --- | --- | --- | --- |
| re-run | 139 | 0 | 120 | 41 |
| v0.3 report | 139 | 0 | 120 | 41 |

**Every one of the 300 verdicts is identical**, mutant for mutant: the same sites,
the same harness verdict, the same suite verdict, zero flips. That is the check
that says the mutator defect did not distort the stage-1 result — a claim worth
making only because it was tested:

* **80 of 300** mutants sit on a line no case executes (statement-span aware), and
  **0 of them were killed** by the harness;
* no `killed-on-a-line-nothing-runs`, no cosmetic-only kills;
* the most frequent differing case accounts for 36 of the 139 harness kills, with
  no single case dominating.

Stage 1 was never exposed to the traceback half of the defect for a simple reason:
none of its 273 cases observes the target's source, path or traceback — the
observation is a return value, or an exception's type and arguments. It was
exposed to the other half (a whole-file rewrite is a different mutant from a
one-line edit), and the re-run shows that on this target that changed no verdict
either.

| | value |
| --- | --- |
| harness | **139/300 = 46.3 %** (95 % CI 40.8–52.0) |
| upstream suite | **259/300 = 86.3 %** (82.0–89.8) |
| harness-only | **0** |
| suite-only | 120 (40.0 %, 34.6–45.6) |
| neither | 41 (13.7 %, 10.2–18.0) |

The conclusion in v0.3 stands, and now it has been re-derived from a mutator whose
mutants are one-line edits.

## 9. What changes in the v0.3 report

* **Stage-2 mutation is no longer saturated, and is no longer quoted as 100/100.**
  It is 62.0 % for the harness against 74.0 % for the suite, with 4 harness-only
  kills — closer to the stage-1 relationship than the defective run suggested, and
  the first stage-2 number that means anything.
* **Stage 1 is unchanged** (139 / 0 / 120 / 41), now on the `ed283159…` case set and
  with zero verdict changes against the published run.
* **The `add_option` finding is no longer suite-only**: the api cases see it, and
  it is an approved difference in the migration report.
* **The two line-number approvals are gone**: the projection makes those cases
  identical, so the migration report has 8 approvals, all of them text or public
  surface.
* The stage-2 case set is 79 cases (`8f04551c…`), and coverage moved with it: lines
  1 225/1 365 = **89.74 %**, branches 661/800 = **82.62 %**, combined **87.11 %**,
  2/89 functions never entered.
* The stage-1 case set is reproducible from code, and rebuilding it is verified
  rather than accidental.

