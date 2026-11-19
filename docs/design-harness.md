# Design: behavior harness (Phase 4)

**The harness gives evidence, not a proof of equivalence.** It runs the same
cases against two versions of a program under a fixed environment and reports
where the observable results differ, where a declared rule explains the
difference, and where it cannot say. A green report means "no difference was
observed in the cases and paths covered" — never "the two versions behave
identically". Everything below is built around making that sentence true,
including the parts that are uncomfortable: uncovered functions are printed in
every report, and anything the harness refuses to judge is counted as
**unsupported**, not silently passed.

## What "same behavior" means

Two versions behave the same **for a case** when, under identical controls, the
same entry point with the same inputs produces the same *observation*:

- the return value, canonically encoded (type and content, not `repr`);
- or the exception's type, module and message;
- plus everything the call wrote to stdout and stderr;
- plus, for CLI targets later, the exit code and a manifest of files written
  (path, size, sha256).

The definition is deliberately narrow and mechanical. "Same behaviour" is *not*
asserted about performance, memory, thread timing, log formatting, or any path
the case set never reaches. Coverage is reported next to every result precisely
so the reader can see the edge of the claim.

## The five result classes

| Class | Meaning |
|---|---|
| **identical** | Canonical observations are byte-identical. |
| **equivalent** | They differ only at path(s) that a versioned, human-reviewed rule explains (uuid, timestamp within N seconds, float tolerance, unordered list, masked field). The masked field is still shape-checked. |
| **different** | A difference that no rule explains. This is the signal the harness exists to produce. |
| **flaky** | The same version, same case, varies between A/A passes in a way no rule explains. Quarantined and reported; never counted as evidence about the change. |
| **unsupported** | The harness refuses to judge — e.g. a case using a custom converter type, or an entry point it cannot observe. Counted and listed, never folded into "equivalent". |

## The three-step noise approach

1. **A/A baseline.** Run the *old* version N times (5 to start) on the same
   case set under the same controls. Any case whose canonical observation
   varies across passes is noise, by definition — nothing changed but the
   clock, the scheduler and the address space.
2. **Noise report.** List every varying case with the path that varied and the
   variation kind (`outcome`, `exception`, `output`, `value`). Cases that vary
   in a way no rule can explain are **flaky** and quarantined: they are never
   used to claim a behaviour change in either direction.
3. **Rules.** Only differences that survive the baseline may be described by a
   normalizer rule, and every rule is reviewed by a human (a rule proposed by
   the A/A report is a *proposal*). The rule file is versioned in the repo and
   its hash goes into every report, so a report can always be traced to the
   exact rule set that produced it.

## Architecture

```
case spec (JSON)  ────────────┐
                              ▼
  workspace A (old)  →  child interpreter (same shim, same limits)
  workspace B (new)  →  child interpreter (same shim, same limits)
                              │
                    canonical observations (content-addressed blobs)
                              ▼
              comparator  →  normalizer rules  →  report (+ rule hash)
```

**Two containers per comparison, run sequentially, alternating order**
(old, new, old, new, …). One container would halve the memory on a 3.83 GiB
Docker VM but would also let one process's environment leak into the other's
observation, which undermines the only controlled variable this design has.
Sequential and alternating is deliberate: if a load-dependent flake exists, it
attaches to a *position* in the sequence and shows up in the A/A noise instead
of being blamed on the change under test. **The two containers differ in
exactly one thing: the workspace tree.** Identical image, identical limits,
identical shim, identical environment — and the report states that.

**Determinism is injected, not hoped for.** A `sitecustomize` shim (clock,
`random`, uuid, hash seed, TZ, locale, cwd, scrubbed env) runs in both children
before the target is imported; the controls that actually applied are read back
from the child and printed in every report. The shim is tested by running a
probe that prints the clock, a random number, a UUID and `hash('x')` twice and
asserting the bytes match.

**The target can loop forever.** A lazy return value is materialised only up to
a fixed bound (1,000 items, marked `truncated`), and a case that never returns is
recorded as **unsupported** with `CaseTimeout` and skipped, because a hang is a
property of the target, not a comparison. `parse.findall("{:tt}", "22:13:20")`
is exactly that case — found by the baseline on its first run.

**Content-addressed observations.** Every observation is canonical JSON whose
sha256 is its address. The golden record is the set of addresses the old
version produced; a report is a mapping from case to address-pair
(old, new) plus a class. Blobs are immutable, so a re-run either reproduces an
address or produces a new one — there is no in-place edit of a measurement.

## Non-goals

- **Not a proof of equivalence.** No claim is made about unexercised paths.
  Coverage is reported as the limit, not as a virtue.
- **Not a semantic diff.** The harness reports *that* a value differs and
  where; explaining *why* (which refactor caused it) is the developer's job.
- **Not fuzzy matching.** No edit distance, no "close enough" strings. The only
  tolerated differences are the declared rules.
- **Not a performance, memory or thread-safety harness.**
- **Not a migration tool.** It measures; it never rewrites the target.
- **No judgement of "better".** A different observation is not a failure until a
  human says the new behaviour is wrong.

## Stage plan and the first target

Stage 1 is a small pure-function library: **`parse` 1.22.4** (MIT, 1 106
source lines, one module, zero runtime dependencies, wheels only). It is a
**calibration target for the harness, not a legacy codebase** — it is small,
deterministic, and boring on purpose, so that a misbehaving harness cannot hide
behind an interesting target. The legacy-modernisation story needs stage 2 or 3
(a genuinely old CLI or web app), chosen only after the harness works.

`parse` also turned out to exercise the shim: its date/time types fill in a
missing year from `datetime.today().year`, so the two no-year formats in the
case set produce a different year without the shim and the same year with it.
The baseline report names those two cases explicitly.

## What a report contains

Counts by class; the masked fields and which rule masked them; unsupported
cases with the reason; coverage of the old version including the uncovered
functions; the noise list from the A/A baseline; the sha256 of the case set and
of the rule file; and the controls that were applied. A report without those
seven things is not a report this harness produced.
