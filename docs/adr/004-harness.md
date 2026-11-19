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
them: **19 of 47 functions were never entered** (129 of 554 statements, ~4 lines
each — the `with_pattern` decorator, the seven `FixedTzOffset` methods, `compile`,
the `Result` dunder methods), and **32 of 47 have at least one unexecuted line**.
The core parse→search→findall path is in the 15 fully covered functions; what is
missing is alternative API surface and error branches. That distinction is the
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
