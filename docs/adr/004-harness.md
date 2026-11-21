# ADR 004: Behavior harness — evidence, not proof

Status: accepted (Phase 4, checkpoint 10)
Date: 2026-11-19

## Context

Phase 4 asks a question the job engine cannot answer: *did this refactor change
what the program does?* The engine can guarantee a step ran exactly once and
committed atomically; it cannot say whether the new code returns the same value.
The harness closes that gap with measurement — and the first thing to be honest
about is that measurement is not equivalence.

Three facts shape every decision below.

1. **Untestable is normal.** No finite case set covers a library. So the harness
   reports coverage next to every result, and treats "not covered" as unknown
   rather than safe.
2. **Noise looks exactly like signal.** A clock, a hash seed, a set iteration
   order and a scheduler can all produce a "behaviour change". The A/A baseline
   exists to make noise visible *before* it can be blamed on the change.
3. **Rules can hide bugs.** A normalizer that masks a whole field will accept
   any change inside it. Every rule is therefore narrow, versioned, reviewed by
   a human, and shape-checked on the masked field.

## Decisions

### D1 — Two containers per comparison, sequential, alternating order

The old and new versions run in separate sandboxes built from the **same
image**, with the same limits, the same shim and the same environment. The only
difference is the workspace tree.

Rejected: both versions in one container. It halves memory (relevant: the
Docker VM is 3.83 GiB) but lets one process's imports, environment mutations and
`sys.modules` leak into the other's observation, which destroys the single
variable the design depends on.

The order alternates (old, new, old, new, …) and the report records positions.
If a flake depends on load, it then appears as A/A noise at a position instead
of being attributed to the new code. Sequential because two concurrent
comparisons would each hold two sandboxes — with Postgres and two interpreter
versions, the VM has room for roughly 8–10 sandboxes, not 4×N.

### D2 — Canonical form is the comparison; its sha256 is the identity

`harness.observation.encode` is total and deterministic:

| Value | Encoding |
| --- | --- |
| `None`, `bool`, `int`, `str` | JSON literal |
| `float` | `{"__float__": "<repr>"}`, special-cased `nan`/`inf`/`-inf` — so `-0.0` and precision survive |
| `bytes` | `{"__bytes__": "<hex>"}` |
| `tuple` | `{"__tuple__": [...]}` — a tuple and a list are different behaviour |
| `list` | array |
| `set`/`frozenset` | `{"__set__": [sorted]}` |
| `dict` (str keys) | object with keys sorted by UTF-8 bytes |
| `dict` (other keys) | `{"__mapping__": [[k, v], ...]}` sorted |
| iterator | `{"__iterator__": [materialised]}` — laziness is not observable, and a half-consumed iterator would otherwise look empty |
| exception | `{"__exception__": {"type", "module", "message"}}` |
| `date`/`datetime`/`time`/`timedelta` | tagged `isoformat`/seconds |
| `Decimal`, `UUID`, `Path`, `Enum` | tagged textual identity |
| any other object | `{"__object__": {"class": ..., "attrs": {sorted}}}`; `opaque: true` when it has no attributes |

Two findings made this list longer than planned, both from the `parse` baseline:

- **`datetime.date` has no `__dict__`**, so the object branch called it opaque
  and a changed date compared **equal**. Every scalar with a stable textual
  identity now has an explicit tag.
- **The shim replaces `datetime.datetime`/`datetime.date`**, which broke
  `isinstance` in the serializer (a real `date` returned by the target is not an
  instance of the frozen subclass). The shim therefore exposes
  `ORIGINAL_DATETIME/ORIGINAL_DATE/ORIGINAL_TIME`, and the serializer matches on
  those. A shim that hides the values it freezes is worse than no shim.

Rejected: storing `repr()`. It embeds memory addresses (`<object at 0x...>`) and
changes between versions for reasons that are not behaviour.

### D2b — Hangs and unbounded materialisation are case outcomes

Two failure modes were found by the first `parse` baseline, and both are now
part of the contract:

- **An infinite iterator hung the harness for 900 s.** `parse.findall("{:tt}",
  "22:13:20")` yields the same zero-width match forever, so materialising the
  return value never finished. Iterator encoding is now bounded at
  `ITERATOR_LIMIT = 1000` items and marks `{"truncated": true, "limit": 1000}`
  — the truncation is *recorded*, so "we saw the first 1000 and there were
  more" is a fact in the report instead of a silent equality.
- **A case that never returns cannot eat the pass.** The child flushes every
  observation, so on a per-case timeout the runner can see where it stopped,
  record that case as **unsupported** with `CaseTimeout`, and resume from the
  next case (up to three attempts). A target hang is a property of the target,
  not a harness failure, and it must not be reported as a behaviour difference.

The general rule: **a case can do anything, including loop forever**, and the
harness's job is to bound its own work and say what it could not observe.

### D3 — Determinism controls, and the proof that they work

Applied via `sitecustomize` (so they run before the target is imported) and
`PYTHONHASHSEED=0` (which must be in the environment, because Python reads it
before any of our code):

`clock_frozen`, `random_seeded`, `uuid_frozen`, `hash_seed_0`, `tz_utc`,
`locale_c`, `cwd_fixed`, `env_scrubbed`.

The environment is an **allowlist**, not a copy of the parent's: a leaked
`DATABASE_URL` or token would be a difference with nothing to do with the target.
Controls are **read back from the child** (`sitecustomize.CONTROLS`) and printed
in every report, so the report states what applied rather than what was intended.

The shim is tested by running a probe that prints the clock, a random number, a
UUID and `hash('x')` twice and asserting the output is byte-identical — and by
running the same probe without the shim and asserting it differs. A shim that
never changes anything would pass the first test and fail the second.

### D4 — Five classes, and what each one promises

`identical`, `equivalent`, `different`, `flaky`, `unsupported` (see the design
doc). The load-bearing one is **unsupported**: custom converter types, entry
points the harness cannot observe, and harness bugs are counted and listed, so
they can never be mistaken for "no difference". `flaky` is separate from
`different` for the same reason — a case that varies between two passes of the
*same* version cannot testify about a change.

### D5 — Rules are data, reviewed, and hashed into the report

Rules live in a config file scoped by path (uuid, timestamp within N seconds,
float absolute/relative tolerance, unordered list, regex mask), are versioned in
the repo, and their sha256 is written into every report. A masked field is still
shape-checked: masking a value must not mask a type change. The A/A report may
*propose* rules; a human accepts them. No fuzzy matching anywhere — an edit
distance threshold is a rule that cannot be reviewed.

### D6 — Coverage is the honest limit, printed not implied

The old version is measured with `coverage.py` under the case set, and the
report lists every function with no executed line. For `parse` 1.22.4 that is
**32 functions** of 47 — a number in the report, not a footnote, because
"0 differences" over 60 % of a library means something different from 0
differences over all of it.

### D6b — Coverage is three numbers, and "uncovered function" means two things

Reporting `percent_covered` as "line coverage" is wrong by three points on
`parse`: 425/554 statements is **76.71 %**, 163/246 branches is **66.26 %**, and
73.5 % is the combined (425+163)/(554+246) that coverage.py's `percent_covered`
actually reports. All three appear in the report, labelled.

"Uncovered function" was also ambiguous, and the ambiguity hid a number:
coverage.py's function data flags a function when *any* line in it is missing, so
"32 of 47 functions uncovered" included functions that were entered and partly
run (`Parser._handle_field` ran 125 lines and missed 30). The report now splits
them, and the split changes the reading:

| Group | Functions | Missing statements |
| --- | --- | --- |
| Never entered | **19 of 47** | **29 of 554** (the `with_pattern` decorator, the seven `FixedTzOffset` methods, `compile`, the `Result` dunder methods, `Parser.format`/`named_fields`/`fixed_fields`, `percentage`, `Match.evaluate_result`) |
| Entered, with unexecuted lines | 13 of 47 | 100 (`date_convert` 28, `_handle_field` 30, `extract_format` 9, `_to_group_name` 8, …) |
| Total with any unexecuted line | 32 of 47 | 129 |

So the harness is not blind to the core: the parse→search→findall path is among
the 15 fully covered functions, and every missing statement outside the
never-entered 29 sits in a branch of a function that did run. Both numbers appear
in the report, with the per-function line counts, because "32 uncovered
functions" alone reads as though two thirds of the library were never executed —
which is not what the data says. That distinction is the
difference between "the harness is blind to the library" and "the harness has not
exercised the parts nobody's refactor is likely to touch".

### D6c — Clock dependence is measured by moving the clock, not by turning it off

Two shim-off passes on the same day agree even for time-dependent cases, because
the real date is the same in both. Shim-off agreement is therefore not evidence
that a case is clock-independent, and an earlier version of this report implied
it was. The experiment that works: run the same case set at two frozen epochs a
year apart. Exactly **3 of 273** cases change — the ones whose format fills in a
missing year from `today()`. The probe table (shimmed vs unshimmed, one arm per
control) is the other half of the evidence: 13 of 14 surfaces differ, and `hash`
is fixed by `PYTHONHASHSEED` in the environment rather than by the shim.

### D7 — Stage 1 target is a calibration instrument

`parse` 1.22.4 (MIT, 1 106 source lines, one module, zero runtime deps, wheels
only) is chosen to be boring: small enough to read, deterministic, no I/O. It
calibrates the harness. It is **not** the legacy-modernisation story; that needs
a genuinely old stage-2/3 target, chosen after the harness works.

Measured bonus: `parse`'s date/time types fill a missing year from
`datetime.today().year`, so the two no-year format cases change with the clock
and are stable with the shim. The baseline names exactly those two as
"decided by the shim" — a free test of D3.

### D8 — Process state is reset before every case; A/A runs in both orders

`random` and the uuid counter are process state. Seeding once per pass makes the
value a case observes depend on how many draws the cases *before* it made, so a
refactor that changes one case's draw count shifts every later case and reads as
a flood of behaviour changes. Sampling a fixed case order hides this by
construction, because every pass couples the same pairs.

Therefore: `reset_controls()` runs before each case in the child (control name
`per_case_reset`), and the A/A baseline runs **both** a fixed order and a
different shuffle per pass. Measured on `parse`: 0 noise in both orders over 5
passes each. That is weak evidence for `parse`, which draws no randomness, so a
unit test demonstrates the mechanism directly instead: with the reset the same
case yields identical `random`/`uuid` values in any position; without it the
values drift.

Rejected: a fresh interpreter per case. It removes every kind of carried state,
including import-level caches, but it is ~30x the process cost and the case set
is 273 cases per pass over 10 passes. Recorded as an option if a target is ever
found to carry state the reset does not cover.

### D9 — An opaque value is `unsupported`, never "equal"

An object with no `__dict__` and no usable slots (C-extension types,
`__slots__` classes, `re.Match`, `re.Pattern`) has no representation the
harness can compare: encoding it as an opaque marker makes two different values
encode identically, and the comparator would then report a change as no change.

Such a value sets `opaque: true` in the observation and the case outcome becomes
`unsupported` with an `UnobservableValue` exception. Measured on `parse`:
**23 of 273** cases, all of them the `evaluate_result=False` variants, which
return a `Result` holding `re.Match` and a `Parser` holding `re.Pattern`. This
is a real reduction in what the harness can say about that API, and it is
visible in the report rather than absorbed into "equivalent".

The recovery path is a declared projection (compare `Result.fixed`/`Result.named`
only), which is a case-spec change, not an encoder change. Deferred.

### D10 — Bounded observation: truncation, cycles, depth and node budgets

`parse.findall('{:tt}', '22:13:20')` never terminates, so "observe the whole
value" is not a total function. Every bound is explicit and recorded:

| Bound | Limit | Recorded as |
| --- | --- | --- |
| Iterator items materialised | 1 000 | `truncated: true`, `limit: "iterator"` |
| Container nesting depth | 50 | `limit: "depth"` |
| Total nodes encoded | 50 000 | `limit: "nodes"` |
| Reference cycles | visited set | `{"__cycle__": ...}` |

A cycle encodes as a marker rather than recursing, so a self-referencing object
does not crash the child — but the marker names the class, not *which* ancestor
the back-edge targets, so a structure that retargets its back-edge encodes
identically. Cyclic values are therefore `undecidable`, not `equal`: the
traversal terminates, the identity of the referent is not observed. (Aliasing is
invisible everywhere in this encoding, not only at cycles — two equal sublists
encode the same whether they are one object or two.) A hit bound sets `limit`, and the
case becomes `unsupported` — a truncated observation is *not* evidence that the
prefixes agree, because two different values can share their first 1 000 items.

Measured on `parse`: 1 truncated case (the `{:tt}` `findall` above), 0
depth/node hits. Checkpoint 11 adds the comparator outcome for two observations
that are both truncated with matching prefixes: **equivalent on bounded
prefix**, a third outcome next to `equal` and `different`, never `equal`.

### D10b — Four comparison outcomes, not two

`equal`, `different` (with a path), `equivalent_bounded` and `undecidable`.
The third exists because two observations cut off at the same bound can agree on
everything inside it and still differ beyond it: calling that `equal` is a claim
about values nobody saw, and calling it `different` would blame the change for
the bound. The fourth is the default for everything the encoder flagged, so "I
could not look" never becomes "nothing changed". `compare` also refuses two
observations whose `case_set_hash` disagree, and `compare_sets` refuses a
missing case.

The compared body excludes `flags`: a node count is bookkeeping, and a
dict-versus-float change must be reported at `$.value`, not at `$.flags.nodes`.

| Comparison outcome | Report class | Where it appears |
| --- | --- | --- |
| `equal`, no rule applied | **identical** | its own line |
| `equal` after a declared rule masked a field | **equivalent** | its own line, with the rule ids and paths |
| `equivalent_bounded` (both truncated, prefixes agree) | **equivalent_bounded** | its own line — never folded into `equivalent` |
| `different` | **different** | its own line, with path, left and right |
| `undecidable` (opaque, budget, unsupported case) | **unsupported** | counted in the `unsupported` line, every reason listed per case |
| a case that varied between two A/A passes | **flaky** | assigned by the workflow, not by a comparison |

The five classes in the design doc therefore stay five lines. `equivalent_bounded`
is a hedged verdict and gets its own line because folding it into `equivalent`
would claim more than the encoder saw; `undecidable` is folded into `unsupported`
in the count but never in the evidence.

### D10c — Rules: one file per target, schema-validated, every rule justified

`harness/rules/<target>.json`, not one shared file: a rule that masks `parse`'s
`__version__` must not silently apply to the next target. Each file carries the
`case_set_hash` it was written for, and a run whose case set differs is refused,
because a rule written for one case set is not a rule for another.

Three requirements, enforced in `load_rules`:

- **Schema validation on load** (`harness/rules/schema.json`, strict: unknown
  fields are errors, so a typo cannot be silently ignored).
- **A mandatory `reason`** of at least 12 characters naming the A/A finding or
  case that motivated the rule. A rule without a reason is a bug being hidden.
- **Hash pinning**: the file's canonical sha256 goes into every report, next to
  the case-set hash.

Masking is shape-checked before it happens, per type:

| Type | What both sides must be |
| --- | --- |
| `uuid` | strings that parse as UUIDs |
| `timestamp` | strings that parse as ISO 8601, within `tolerance_seconds` |
| `float` | floats on both sides (an int against a float is different), within absolute or relative `tolerance` |
| `regex_mask` | strings that fully match `pattern` |
| `unordered_list` | lists of equal length and equal as multisets |

A refused rule does not stop the walk (a broad `$.value` rule must not shadow a
narrow `$.value.id`) and leaves the values unmasked, so an uncheckable rule can
never hide a difference. Path syntax is dotted with `*` for one segment; a key
containing `.` cannot be named, and such a rule is refused rather than silently
meaning something else.

`parse` ships an empty rule list. That is the correct result for a target with 0
A/A noise, and it is recorded as such rather than left absent, so "no rules" is
distinguishable from "nobody wrote any".

### D10d — A crash is a behaviour; only a markerless death is a harness bug

A refactor that introduces `os._exit`, an abort or a segfault has changed
behaviour, and it is exactly the kind of change a case set can catch cheaply. So
a child death is classified by how it died, not lumped into "harness bug":

| Death | Recorded as | Why |
| --- | --- | --- |
| exits with a status | `exception`, `{"__process__": {"kind": "ProcessExit", "exit_code": n}}` | the exit status is observable |
| dies on SIGSEGV/SIGABRT/… | `exception`, `ProcessSignal` with the signal and its name | the signal is observable |
| cannot import the target | `exception`, `{"__import__": {...}}` for **every** case | the module not importing is the most complete behaviour change there is |
| SIGXCPU | `unsupported (CpuBudget)` | a budget, not the target |
| SIGKILL | `unsupported (ResourceKill)` | a property of the limits (OOM or the hard backstop) |
| no begin marker at all | **raise** | the harness itself failed |

The begin marker is what makes this possible: the child flushes the case id it is
about to run, so a death can be attributed to a case even when no observation was
ever written. A death with no marker cannot be attributed, and that — and only
that — is a harness bug.

The first mutation run crashed on a mutant that deleted a module-level name and
therefore broke the import. That is how this decision was found: the runner
raised "child exited 1 before beginning any case" and the experiment stopped,
which would have hidden a perfectly good kill. An import failure is now a
finding for every case, and one bad mutant can no longer abort the run.

### D10e — The observation schema is versioned, and the version is in the path

Checkpoint 11's `EncodedValue` fix changed every blob's bytes without changing any
observed value (the parent had been re-encoding the child's encoded value, so each
tag was escaped one level down). That is a schema change, not a case change, and
the two must not be confused: `observation_schema_version` is now in every
observation, every report and the blob path
(`<root>/v<schema>/<case_set_hash[:8]>/<sha[:2]>/<sha>.json`).

The verification is the point: regenerating the **checkpoint-10 case set** under
schema v2 reproduced its headline numbers exactly — 238 return, 12 exception, 23
unsupported, 273 blobs. A schema bump that changed a value would have shown up
there. `CHANGELOG.md` records the bump and why.

### D10f — Case budgets: soft per case, hard per pass, restarts counted

The soft limit is per case: "CPU used so far + budget". The hard limit must be
set once (POSIX can lower a hard limit but never raise it), so it has to cover the
legitimate total CPU of the whole pass or it would kill a healthy late case. It is
`2 × per_case × cases`, floor 60 s: for 10 s per case over 273 cases that is
**5 460 s**, roughly 90× what a full `parse` pass actually uses. It exists to
bound a process that ignores SIGXCPU, not to be a case budget.

Restarts count against the same three-attempt budget as timeouts: each budget kill
consumes one attempt, the pass resumes after the killed case in a fresh child, and
if the attempts run out the un-reached cases are recorded as
`unsupported (NotRun)` so the counts still add up to the case count. Measured
today: the whole mutation run consumed zero restarts on unmutated code, and
exactly one per CPU-budget kill.

### D10g — Rule usage is reported, because a close call is a warning

A rule that masked a field where nothing differed is silent; a rule that masked a
field differing by 99 % of its tolerance is one refactor away from masking a real
change. Every report now carries, per rule: how many observations it matched, the
largest observed difference, and the tolerance that permitted it (or a note that
the check is shape-only). The number is not a verdict — it is the evidence a
reviewer needs to decide whether the tolerance is still reasonable.

Paths may now quote a segment (`$.value["a.b"]`) so a key containing the separator
can be masked deliberately instead of being unmaskable. An unquoted bracket
segment, or an empty segment, is refused rather than silently reinterpreted.

### D10h — Projections before mutation, and what truncation means

23 of 273 cases (8.4 %) were `unsupported` at checkpoint 11, all in
`evaluate_result=False`, because the returned `Result` holds a `re.Match` and a
`re.Pattern`. Mutation testing would pile survivors up there and every one would
need an explanation, so the fix came first: a **projection registry**
(`harness/projections.py`) with an allowlist of named views, selected by name in
the case spec (`"projection": "evaluate_result"`). A case spec cannot run code; it
can only name one of the reviewed projections. The projection is part of the case
body, so it is part of the case-set hash.

Measured: `unsupported` fell from **23 to 0**, return rose from 238 to 261, and
line coverage rose from 425 to 426 statements.

`expect: finite | stream` decides what truncation means. A `findall` is a stream by
definition: a bounded prefix is the honest observation, and two matching prefixes
are `equivalent_bounded`, never `identical`. A finite case that hits the item
limit is `unsupported (LimitHit)` — the harness did not observe the value it was
promised. Truncation is deliberately **not** turned into a projection that reports
identical.

### D11 — The case set's identity is its hash, not a version integer

`case_set_version: 1` cannot detect an edited case file, and comparing a run
against blobs recorded from a different case set would attribute a case-set edit
to the code under test. The case file's sha256 is computed over the sorted
canonical body of every case (`case_set_hash`), written into the case file,
every observation, every blob path (`<root>/<hash[:8]>/<sha[:2]>/<sha>.json`) and
every report. `run_pass` takes `expected_case_set_hash` and raises on mismatch
rather than comparing.

### D12 — Case budget: CPU time where the platform has it

A wall-clock timeout measures the host, not the program: under load, a
slow-but-deterministic case becomes `unsupported` and the report cannot tell a
hang from a busy machine. So the child sets `RLIMIT_CPU` before each case, with
the soft limit at "CPU used so far + budget". A case that loops inside C (a regex
backtracking, a tight numeric loop) is killed by **SIGXCPU** at its budget, which
a Python-level timeout cannot do without killing the process from outside.

The hard limit is a pass-wide backstop set once, not `soft + 1`: POSIX lets a
process lower a hard limit but never raise it again, so a per-case hard limit
would make the second case unmeasurable. Soft alone terminates the case, which is
the observed behaviour, not an assumption:

| Check | Result |
| --- | --- |
| `re.match(r"(a+)+b", "a"*40+"c")` with soft = used+2, `python:3.12-slim` | killed, exit 152 (128+SIGXCPU), "CPU time limit exceeded" |
| the same call with a SIGXCPU handler installed | handler runs, then the process exits by its own choice |
| a 3-case pass with one such case, budget 3 s | case recorded `unsupported (CpuBudget)`, other two cases recorded, pass resumes, 5 s total |

The runner attributes the kill with a **begin marker**: the child writes the case
id it is about to run and flushes, so a dead child leaves "this case was in
flight" behind even though no observation was written. A death that is not a
budget signal is a harness bug and is raised, never recorded as a target finding.
On Windows `resource` is absent, the wall clock is the only bound, and the report
says which budget was used. A case that exceeds its budget in some passes and not
others is `flaky`, not `unsupported`, because the disagreement is the finding.

## D10i — Mutation and refactor evaluation (stage 1, `parse` 1.22.4)

Two questions, two tools. Neither has a target number: a pre-set kill percentage
would be a number to tune against, not a measurement.

**Does the harness notice wrong behaviour?** An AST mutator in the repository
(`src/harness/mutate.py`) applies one edit per mutant from seven operator
families — comparison and arithmetic swaps, integer `±1` (both directions),
boolean flips, `return None`, statement deletion, slice-bound `±1` — to 839
enumerated sites in `parse/__init__.py`. Sites are enumerated depth-first in
source order, so a seed reproduces a sample exactly. A mutant is generated by
re-parsing the edited source: a mutant that does not parse is a mutator bug, not
a survivor.

The sample is stratified (at least three per operator, the rest proportional),
seeded (12) and time-probed first: one pass costs ~1.0 s and one mutant ~6.7 s
(harness pass + upstream test run), so 300 mutants is ~35 min. The plan is
written before any result, and results are appended as they are produced.

| Oracle | All 300 | On the 203 mutants whose line the cases execute |
| --- | --- | --- |
| Harness | 139 = 46.3 % | 138 = 68.0 % |
| Upstream test suite (`parse-1.22.4/tests`, 109 passed) | 42 = 14.0 % | 10 = 4.9 % |
| Either | 181 = 60.3 % | 148 = 72.9 % |

Both columns are published because the difference is the finding: 97 mutants sit
on a line no case reaches, and no observation could distinguish them. Survivors
are split three ways — **96 not covered**, **63 covered but not observed**, **2
equivalent** — and every covered survivor carries a written reason (the sign and
base branches of `int_convert`, `%p` AM/PM, the missing-year default, the
width/precision branches of `extract_format`, and so on). The two equivalents are
dead defaults: the module-level `search()`/`findall()` wrappers always pass `pos`
and `endpos`, so `Parser.search`'s `pos=1` and `findall`'s `is None` check cannot
be reached through the public API.

The covered-but-not-observed group is the actionable one and it is a **case-set**
result, not a comparator failure: the case set has no `%p` date, no signed fixed
field, no `=` alignment, no repeated named field, no two-datetime format. Adding
those cases is stage-2 work with a target that needs them.

**Does the harness cry wolf?** Twelve harmless refactors — six mechanical
(`ruff format`, definition reordering, a libcst local rename, `%` → f-string,
loop → comprehension, extract method) and six hand-written (local renames, branch
restructuring, docstring rewrite, constant hoisting, comment churn, a class
alias) — produce **272 identical / 1 equivalent_bounded and 0 different**: zero
false positives. A single seeded bug hidden inside one of them (`"-"` → `"+"` in
the sign conversion) is `different` on 4 cases, so the clean result is a
measurement of the refactors, not of a comparator that says nothing.

## D10j — CI reports its own counts

Job logs need admin rights on this repository and the uploaded JUnit artifacts
need a token, so both jobs publish their counts where a plain `GET` reaches them:
a `::notice` annotation (`junit/ci`, `junit/sandbox-hostile`) and a commit
status. A red run additionally emits `::error` annotations naming the failing
tests. `scripts/ci_status.py` does this from the JUnit XML it already parses for
the run page, so the numbers cannot drift from the artifact.

Latest green run (`f58b74d`): `ci` **366 collected / 340 passed / 26 skipped / 0
failed**; `sandbox-hostile` **25 / 25 / 0 / 0**. The 26 skips are 18 hostile-suite
(needs the docker SDK + `ENGINE_SANDBOX_TESTS=1`), 7 install-hostile (same gate)
and 1 mutator test that needs the unpacked target tree (`.harness/`, git-ignored).
The CPU-budget test is POSIX-only; it is skipped on Windows and **runs on CI** —
the counts show 340 passed where the Windows run has 338, and an earlier red run
failed inside that test, which a skipped test cannot do.

## D10k — The sandbox transport: copy-in, copy-out, no host mounts

A pass is `python -m harness.child plan.json out.jsonl`. Until this checkpoint it
ran as a **host subprocess** (`subprocess.Popen` in the checkout directory, no
container, no network isolation, no limits). Mutation, refactor and verification
runs all went through that path, which contradicts ADR 003's claim that untrusted
code runs in the sandbox. The transport closes that.

The design follows ADR 003 D4 exactly: **no host mounts**. The workspace is built
in memory and copied in as a tar stream into the container's size-capped tmpfs
(`/workspace`), the child runs there, and the results are copied out as a tar
stream. Inside the tmpfs:

```
/workspace/plan.json        the pass plan
/workspace/src/harness/*    the child itself (so the harness version is pinned per run)
/workspace/shim/*           sitecustomize, the determinism shim
/workspace/checkout/*       the target tree under test
```

The container is the Phase 3 profile, unchanged: `network_mode=none`,
`read_only` root, `cap_drop=ALL`, `no-new-privileges`, non-root `65534`, and
cgroup limits (memory, pids, CPU quota, nofile, fsize). Nothing is mounted from
the host in either direction.

**How observations come out of a dead payload.** The sandbox runner's supervisor
runs the payload as a *child* and prints its exit code on a nonced marker line
before holding the container open for the copy-out. A child killed by its
`RLIMIT_CPU` (SIGXCPU) therefore does not take the container with it: the marker
arrives, the copy-out runs, and the harness gets the observations written before
the kill **plus** the begin marker that names the in-flight case. That is the same
attribution mechanism as the host path, and the reason the per-case bound is an
in-container `RLIMIT_CPU` rather than a container wall-clock kill: the latter
destroys the tmpfs and with it the evidence. Both cases are tested
(`tests/test_harness_sandbox_transport.py` for the bookkeeping,
`tests/test_harness_sandbox_hostile.py` for containment and kill recovery, in the
`sandbox-hostile` CI job).

**Measured (2026-11-20, `sandbox-hostile` CI job).** 300/300 mutants get the same
verdict through the container as through the direct path; the observation stream
for the 273-case set is byte-identical; overhead is **15.12 s per pass** against
~1.2 s of work (~50 minutes for the full 300-mutant gate, so CI runs a smaller
default and the full run is recorded in
`docs/results/sandbox-transport-20261120.md`). The hostile target — write outside
the workspace, socket connect, 200 sleeping children — is contained and recorded,
and the suite reports 27/27 in that job.

**Regression gate.** Moving the harness must change nothing. `scripts/sandbox_gate.py`
re-runs the sampled mutants through the container and compares, per mutant, with
the verdicts the direct path recorded, and separately compares the observation
stream for the unmutated case set byte for byte. It also reports the overhead per
pass. The gate is a required step of the `sandbox-hostile` job; its result is
published as an annotation because job logs need admin rights.

**Hostile case.** A target that writes outside its workspace, opens a socket, and
forks without limit must be contained *and* the attempt must be recorded as an
ordinary observation — the point is not that the harness survives, it is that a
hostile target is data, not an outage.

## D10l — The oracle was inverted, and the audit that caught it

The first 12a report said the harness and the upstream suite had **zero**
overlap and that the union was 60.3 %. That was an artefact: `_run_tests` returned
`returncode == 0` and the caller stored it as `tests_killed`, so a passing suite
was recorded as a kill. The corrected table:

| | Tests killed it | Tests missed it |
| --- | --- | --- |
| Harness killed it | **139** | **0** |
| Harness missed it | **120** | 41 |

Three lessons, all now enforced:

- **A verdict function must be named after its polarity.** The function is now
  `_run_tests` returning `tests_killed`, and the docstring says which way the
  boolean points.
- **Every stored verdict carries its witness.** `tests_note` holds the pytest
  line for that mutant, and the migration that flipped the stored values asserted
  the note agreed with the flipped value on all 300 rows before writing.
- **A surprising result is a bug report, not a finding.** Zero overlap between
  two oracles drawn from the same inputs is not a plausible measurement; the
  review caught it, and the check that would have caught it locally is now part
  of the report: the contingency table is printed, not just the marginals.

The corrected conclusion is the more useful one: **the harness adds nothing to
the upstream suite on this target** (harness kills ⊂ test kills), and the 120
tests-only kills say exactly where the case set is blind. A case set that only
repeats a mature suite is a regression detector, and saying so is cheaper than
pretending it is an oracle.

## D10m — Tuning protocol: first pass, overfit run, held-out run

Adding cases for the survivor clusters raises the kill rate by construction, so
the measurement is not the tuned number. Three runs are reported separately:

| Run | Case set | Mutants | Kill rate |
| --- | --- | --- | --- |
| First pass (headline) | 273 cases | 300 (seed 12) | 139/300 = 46.3 % |
| Tuned, same mutants (overfit) | 307 cases | 300 | 180/300 = 60.0 % |
| Held-out, original cases | 273 cases | 150 (seed 99, 539 sites) | 58/150 = 38.7 % |
| Held-out, tuned cases | 307 cases | 150 | 83/150 = 55.3 % |

The held-out sample excludes the 300 sites the first sample touched, so no
tuning case was written with those sites in view; it is stratified by operator
and drawn with its own recorded seed. The gain is +16.6 points held-out against
+13.7 points on the tuning set — the intervals overlap, so the honest reading is
"the new cases generalise", not "tuning doubled the kill rate".

## D10n — The 41 mutants both oracles missed

The 41 mutants that survived both the case set and the upstream suite are the only
candidates for a true equivalent mutant, so each was read and probed by hand:
build the mutant, call an API directly, compare the unmutated and mutated result.

| Verdict | Count |
| --- | --- |
| Docstring deletion (`__doc__` only) | 7 |
| Real behaviour, observed by neither oracle | 33 |
| No difference found under any probe (candidate equivalent, **not** proven) | 1 |

The probe evidence is in `docs/results/mutation-both-missed-20261120.md`: month
names (`01/Feb/2024` → `01/Jan/2024`), `FixedTzOffset.__eq__`, `tzname`,
`repr(Parser(...))` on a long format, `Parser.search`'s default `pos`, timezone
minute 59, the fill-character escape, duplicated-name renaming. Every one is a gap
in **both** oracles. 32 of the 41 sit on a line no case and no test executes, which
is the honest backlog: writing cases for unexecuted lines is a different exercise
from tuning a branch, and this sample cannot tell them apart.

One survivor is labelled a *candidate* equivalent rather than an equivalent,
because a probe is an experiment, not a proof.

## D10o — A canary caught the test oracle shadowing the mutant

The 12a polarity bug survived because nothing tested the evaluator end to end.
`tests/test_harness_canary.py` now does: an obviously broken mutant must be killed
by **both** oracles, a comment-only change must be survived by both, and an
unmutated tree must not read as killed.

Writing it immediately found a second bug. `_run_tests` invoked pytest on the
tests directory, and pytest's default `prepend` import mode puts that directory at
the front of `sys.path`. When the tests sit next to the module under test — the
canary's layout — `import canary` resolved to the **unmutated** copy and the
oracle silently graded the wrong tree. `parse` was unaffected only because its
tests live in a subdirectory. The fix is `--import-mode=importlib`, which stops
pytest touching `sys.path` at all, so the mutant on `PYTHONPATH` wins. The parse
verdicts were re-checked against three known rows after the change and are
unchanged.

The lesson is the same one as D10l from the other side: an oracle that is not
tested is an oracle you are guessing about.

### D10p — A CLI case observes a program, and its environment is a fixed list

Stage 2 changes a command line, so a stage-2 comparison has to run a command
line. `Case` gained `kind: call | cli`, `argv`, `stdin` and `files`; a `cli` case
runs the target program as a subprocess in a fresh directory holding exactly its
input files, and the observation is a value: exit status, signal number, stdout,
stderr, and a manifest of the files it wrote (relative path, size, sha256). A
signal death is part of the value, not an exception, because for a program
"killed by SIGSEGV" is behaviour.

The environment is fixed rather than inherited: `HOME` and `XDG_CONFIG_HOME`
point at empty directories inside the case directory (a linter reads
`~/.pycodestyle`), the project config is the case directory, `COLUMNS=80`,
`LANG/LC_ALL=C.UTF-8`, `PYTHONIOENCODING=utf-8`, stdin is the case's text or
closed, there is no TTY, and `PATH` is `/usr/local/bin:/usr/bin:/bin`.
`--benchmark` is declared `unstable` and `--jobs > 1` `unordered`; neither is
normalised, because normalising them would hide exactly the kind of change stage
2 exists to find.

No timing goes into the value: a duration is nondeterministic by construction and
would need a rule on every case. The pass duration belongs in the report.

### D10q — An approval is bound to the diff, never to the case

A migration changes some output on purpose. A rule would say "these two are
equivalent", which is false for `--help`; an approval says "these two differ,
here is the diff I accepted, here is who accepted it". So
`harness/approvals/<target>.json` records `case_id`, `path`, `reason`,
`reviewer`, `date` and the sha256 of the *difference*. A matching case and path
with a different digest is a **stale approval** and the case goes back to
`different`. Approvals are never counted as equivalents, the file's hash is in
every report, and `verify-change` prints approved and unapproved differences
separately, so "0 unapproved" is a claim a reader can check against the table.

### D10r — An oracle is checked before its numbers are quoted

`scripts/oracle_canary.py` asserts, for every pinned target, that the unmutated
tree passes and that a known-broken mutant fails, and writes the result to
`docs/results/oracle-canary-<date>.json`. A suite that is red to begin with
"kills" everything; a suite that cannot fail kills nothing. Both are silent
without this check.

The same script documents a trap: `pycodestyle`'s **PyPI sdist ships `tests/` but
not the `testing/` package those tests import**, so the sdist suite fails
collection with 8 `ModuleNotFoundError`s. The oracle therefore uses the pinned
GitHub tree at the release tag (`763 passed, 12 skipped`), while the module under
test still comes from the sdist. Quoting the sdist as the oracle would have
produced "every mutant survived", with the same shape as D10l: a number that
looks like a finding and is an infrastructure error.

`--import-mode=importlib` is required for the same class of reason: pytest's
default `prepend` mode puts the tests directory first on `sys.path`, so a mutated
module can be shadowed by an unmutated copy beside the tests and read as a
survivor.

### D10s — Nothing about the container's environment is inherited from the host

`sandbox_env()` originally copied `PATH` from the host when present and defaulted
it only when absent. On a Windows host that hands `C:\Windows\System32;...` to a
Linux container, whose readiness loop then cannot find `sleep`; the copy-in exec
exits 127 and the failure looks like a transport bug. On a Linux CI runner the
host `PATH` is valid POSIX, so every CI run before this was green for the wrong
reason.

`PATH` is now `SANDBOX_PATH = "/usr/local/bin:/usr/bin:/bin"` — an override, never
a default. A container is always Linux; nothing about its environment may be
inherited from the machine that started it. Pinned by
`test_the_container_path_is_never_inherited_from_the_host`.

### D10t — Two fixed waits were 94 % of the sandbox overhead

The first overhead measurement said 15 s per pass and blamed the tar transport.
It was two waits, and the breakdown that "explained" them did not add up — which
was the clue.

* `container.wait(timeout=5)` in `_wait_for_done`. docker-py passes `timeout` to
  `_post(url, timeout=...)`: a **client** request timeout, not a daemon wait. The
  supervisor holds the container open on purpose (so the tmpfs workspace survives
  for copy-out), so every call blocked the full slice and the marker event was
  only examined afterwards. Cost: 5 s, or 10 s when the payload crossed a slice.
* `reader.join()` **before** closing the attach socket. The socket stays open as
  long as the container is alive, so `join` waited its whole 10 s timeout.

The wait is now on the local event (`0.02 s` granularity) with a `0.5 s` liveness
re-read of the container state as the fallback exit detector, and the reader's
stream is closed before it is joined. Overhead went **15.12 s → 0.91 s per pass**
(300-mutant gate: 76 min → 13 min, still 300/300 and byte-identical), and a
sandboxed `/bin/true` run with the real workspace went 11–16 s → 1.45 s.

Two rules came out of it:

* **A timing breakdown must decompose one measured total.** Three independent runs
  of overlapping work are three measurements, not a decomposition; `-X importtime`
  put the child import at 1.33 s, not the 5.02 s the subtraction suggested.
* **A stub must model the state the code reads.** The exit detector now reads
  `State.Status`, so `FakeContainer` had to grow a `Status` and say whether it
  models a running container — the alternative was a suite that passes while the
  detector is wrong.

### D10u — Coverage of a grandchild needs `COVERAGE_PROCESS_START`

A CLI case runs the target as a subprocess of the harness child, so coverage of the
child sees none of the target's lines. The hook lives in the shim's
`sitecustomize` (already on `PYTHONPATH`), guarded by the environment variable, and
the runner writes an explicit `--rcfile` because the target's own `setup.cfg`
configures coverage (`plugins = covdefaults` in `pycodestyle`) and would otherwise
be read from `cwd=checkout`. Data files are `parallel` and merged with
`coverage combine`, whose data-file **prefix** must match what the subprocesses
wrote — pointing it at a different name silently merges nothing and reads one
subprocess's file as if it were the whole run. The report states the check that
catches this: one case must be strictly lower than the full set, and `_main` must
appear.

### D10v — A mutant is a minimal text edit, never a re-print of the tree

`apply_site` returned `ast.unparse(tree)`. Every other property held: the tree
parsed, exactly one operator changed, the site was addressable. The file was
rewritten. `pycodestyle.py` went from 2 717 lines to 2 095 on **every** mutant, so
every line number and every echoed source line moved.

That is not a nuisance, it is a broken oracle. Two of the 77 CLI cases print a
traceback and a warning that name the target's own file and line, so *every*
mutant differed on exactly those two cases: 77 and 75 of 100 respectively, and 38
mutants differed on nothing else. Fifteen of the 100 "kills" landed on lines no
case executes, which is only possible if the difference came from the file layout
rather than from the mutation.

A mutation is now a splice on the original bytes (byte offsets, because `ast`
columns are UTF-8 byte offsets), with the invariant enforced after every edit:
**one contiguous region, on the site's own line**. `mutate()` refuses a result
that touches more. The one-line `ast.unparse` version is the canary: applied to an
unmutated module it must be killed by nothing, and the first version of the
evaluator killed it everywhere.

Two side effects worth recording, because both are corrections rather than
regressions:

* a chained `a < b < c` or `a and b and c` is one site and now flips **one**
  token, so the mutant is a one-token change and the diff stays on one line;
* `delete_stmt` uses `pass` for a statement alone in its block and `...` for one
  that shares a line with an `if` header, instead of producing a file that does
  not parse.

### D10w — A CLI observation must not contain the target's own source

Same class of defect as the staged-path bug in `verify_change` ("two trees, one
path"), one level down: the observation included the target's *file path, line
numbers and source text*, through tracebacks and through warnings.

Projecting them away is not a convenience. Left in, they are three false
positives at once: a tree staged anywhere else differs, a patch that adds two
lines differs, and every mutant differs — the last of which is indistinguishable
from a perfect case set.

`harness.projections.canonicalise_stream` keeps the exception type, its message
and the frame names, drops paths, line numbers, the echoed source line and its
caret line, and folds CRLF to LF (the same target prints `\r\n` on Windows and
`\n` on Linux through a pipe, so a raw stream cannot be compared across
platforms). It is named `cli_stream_projected` in every CLI observation's
`controls`, so a report says what view it compared. The observation schema went
to 3: the values changed, and the version is what stops two encodings being
compared.

The two approvals that existed only because a line number moved are gone, and the
`--version` prefix bug that the cases caught earlier is still caught.

### D10x — A program cannot show an API nobody calls, so there is a `kind: "api"`

The one difference the upstream suite saw and the harness did not was
`get_parser().add_option`: an attribute on a returned object. No CLI run reaches
it, and a single call case returns the object without describing it.

Case kind `api` imports the module and snapshots the surface: public names,
`inspect.signature` for callables, class bases and attributes, stable scalar
values, the first line of the module docstring, and — when the case names a call —
the type, bases and attributes of what that call returns. Values are compared like
any other value; anything whose `repr` carries an address is reduced to its type
name so the snapshot stays deterministic.

On the migration it reports `OptionParser` → `ArgumentParser` and the exact
attribute set that changed, as an approved difference with a witness. That is the
report saying what the suite found, instead of leaving it in a footnote.

Two decided limits, both learned by running it:

- **Addresses are not values.** An object whose `repr` carries an address becomes
  its type name; otherwise the snapshot differs in every process.
- **Neither are paths.** A module can resolve a directory at import time —
  `USER_CONFIG` is `expanduser('~/.config/pycodestyle')` — so a snapshot contains
  the path of the copy that was imported, and two copies of unchanged code differ
  in a constant nobody touched. `HOME`, `XDG_CONFIG_HOME`, the temp directory, the
  workspace and the case directory are replaced by markers, longest first, and the
  markers are named in the observation's controls. This was found by a
  behaviour-preserving canary, not by either oracle: neither oracle was wrong, the
  observation was.

A snapshot is still not an exercise: it shows that `add_argument` exists with a
signature, not that it behaves. Two `register_check` contracts remain the suite's
alone for that reason, and the report says so.

## Consequences

- A report must carry: class counts, masked fields + rules, unsupported cases,
  coverage + uncovered functions, A/A noise, the case-set hash and the rule-file
  hash, and the applied controls. A report missing any of them is not valid.
- The comparator, normalizer and workflow (Task 7/8) build on the observation
  blob format fixed here; changing D2 later invalidates every stored golden
  blob, so the encoding is versioned and every blob is keyed by the
  `case_set_hash` it came from.
- Custom-converter `parse` cases are **unsupported** for now, by decision, not
  by accident.
- The sandbox is not yet in the loop at checkpoint 10: passes run in child
  interpreters on the host with the same shim. Moving them into the sandbox
  (Task 8) must not change the observation format — that is the invariant.
