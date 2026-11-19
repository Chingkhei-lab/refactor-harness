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
**33 functions** out of the module — a number in the report, not a footnote, because
"0 differences" over 60 % of a library means something different from 0
differences over all of it.

### D7 — Stage 1 target is a calibration instrument

`parse` 1.22.4 (MIT, 1 106 source lines, one module, zero runtime deps, wheels
only) is chosen to be boring: small enough to read, deterministic, no I/O. It
calibrates the harness. It is **not** the legacy-modernisation story; that needs
a genuinely old stage-2/3 target, chosen after the harness works.

Measured bonus: `parse`'s date/time types fill a missing year from
`datetime.today().year`, so the two no-year format cases change with the clock
and are stable with the shim. The baseline names exactly those two as
"decided by the shim" — a free test of D3.

## Consequences

- A report must carry: class counts, masked fields + rules, unsupported cases,
  coverage + uncovered functions, A/A noise, the case-set hash and the rule-file
  hash, and the applied controls. A report missing any of them is not valid.
- The comparator, normalizer and workflow (Task 7/8) build on the observation
  blob format fixed here; changing D2 later invalidates every stored golden
  blob, so the encoding is versioned by `case_set_version`.
- Custom-converter `parse` cases are **unsupported** for now, by decision, not
  by accident.
- The sandbox is not yet in the loop at checkpoint 10: passes run in child
  interpreters on the host with the same shim. Moving them into the sandbox
  (Task 8) must not change the observation format — that is the invariant.
