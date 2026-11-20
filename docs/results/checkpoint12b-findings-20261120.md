# Checkpoint 12b — answers to the open findings

Six findings were raised against the 12b pre-work. Each one is answered here with
a measurement, not an argument. Where the measurement contradicts what the
previous document said, the previous document is wrong and this one replaces it.

Everything below was measured on this host (Windows 10 26300, Docker 28.3.2 on
WSL2, `[wsl2] memory=4GB processors=4`, image `python:3.12-slim`) on 2026-11-20
unless it says otherwise.

---

## Finding 1 — the overhead was two fixed waits, and the breakdown was wrong

**You were right on both counts.** The arithmetic did not sum, and the cause was a
poll interval, not a size effect. There were **two** fixed waits, and together
they were ~94 % of the overhead:

| fixed wait | where | why it cost the full slice | cost |
| --- | --- | --- | --- |
| `container.wait(timeout=5)` | `_wait_for_done` | docker-py passes `timeout` to `_post(url, timeout=...)` — a **client** request timeout, not a daemon wait. The supervisor holds the container open on purpose, so every call blocked the whole 5 s before the marker event was looked at | 5 s, or 10 s when the payload crossed a slice boundary |
| `reader.join()` before closing the attach socket | `run()` tail | the attach socket stays open while the container is alive (same supervisor sleep), so `join` waited its whole timeout | 10 s |

That is why the numbers fell into two groups 5 s apart (`11.25` vs `16.22`) and
why "child import" also measured 5.02 s: **each probe paid one or two slices**,
so the probes were measuring the same waits three times over, not three different
phases. `0.75 + 10.49 + 5.02 + 1.16 = 17.42` did not have to equal 15.16, because
only `control_direct_seconds` and `control_sandbox_seconds` were a matched pair;
the rest were independent runs of overlapping work. Reporting them as a
decomposition was the mistake.

### The fix

```python
# src/engine/sandbox.py
_MARKER_POLL_SECONDS = 0.02     # wait on the local event, which wakes instantly
_LIVENESS_POLL_SECONDS = 0.5    # re-read container state only when no marker came

def _wait_for_done(container, done_event, deadline):
    next_liveness = 0.0
    while True:
        if done_event.wait(_MARKER_POLL_SECONDS):
            return "marker"
        now = time.monotonic()
        if now >= next_liveness:
            next_liveness = now + _LIVENESS_POLL_SECONDS
            if _container_stopped(container):   # explicit evidence only
                return "exited"
        if now >= deadline:
            return "timeout"
```

and `reader.stop()` closes the attach socket before `reader.join()`. The marker
has already delivered the exit code, so the rest of the stream is not needed.

### After

| measurement | before | after |
| --- | --- | --- |
| one sandboxed `/bin/true` run with the real workspace | 11.3 - 16.3 s | **1.45 s** |
| harness pass overhead (direct vs sandbox, 273 cases) | 15.12 s | **0.91 s** |
| 300-mutant sandbox gate | 76 min | **13 min** |
| gate verdicts / bytes | 300/300, identical | **300/300, identical** |

The gate at 300 mutants now runs in the time 60 used to take
(`docs/results/sandbox-gate-20261120.json`: `verdict_agreement 300/300,
byte_identical true, overhead 0.91 s/pass`, 139 killed / 161 survived).

That is on this host. On a cold GitHub runner the same code path measured
**~12 s per mutant** (53 mutants in 10 min 43 s), ten times slower per pass than
here, so CI runs a 30-mutant seeded sample and cites the 300-mutant artifact for
the full number. The runner's per-pass cost is a fact about the runner, not about
the transport change: the two fixed waits are gone in both.

The first CI attempt at 300 exited with status 1 after 53 mutants **without a
traceback**, and the job log is admin-only, so the cause was not visible. The gate
now catches a failing mutant, prints its traceback and the site, and continues;
it also prints `GATE FAILED: ...` with the first disagreements. A gate that can
fail invisibly is not a gate.

### Where the 1.45 s actually goes (instrumented, not inferred)

| phase | seconds |
| --- | --- |
| container create + attach + start | ~0.55 |
| copy-in (213 KB, 23 files) | 0.17 |
| wait for the marker | 0.02 |
| cgroup OOM counter read | 0.17 |
| copy-out | 0.16 |
| teardown (kill + remove) | 0.22 |

Sums to ~1.3 s, and three consecutive `run()` calls measured 1.45 / 1.50 / 1.38 s.
Copy-in is now demonstrably size-insensitive in the range that matters: 213 KB is
0.17 s, and it was never the 10 s.

### `-X importtime` on the child, as asked

```
import time:    54178 |   427511 |   harness.observation
import time:   100630 |  1330798 | harness.child
```

`import harness.child` is **1.33 s** cumulative in a cold `python:3.12-slim`
container, of which `harness.observation` is 0.43 s (`typing`, `pathlib`,
`dataclasses`, `json`, `hashlib` are most of it). So the 5.02 s in the old table
was one wait slice, not import cost. The honest import number is 1.33 s, paid once
per pass, and it is the largest remaining fixed cost of a pass.

### The derived image: measured, and deliberately not done

The suggestion was to bake the harness, the shim and a clean target into a
content-addressed derived image so each mutant copies in only the mutated file.
With the waits fixed, the arithmetic has changed: copy-in is 0.17 s of a 1.45 s
run, so an image would save at most ~0.2 s per mutant (14 %) and add a build
pipeline, an image-reuse lifecycle and a cache invalidation rule. It is written
down as a deferred optimisation in ADR 004 D10k with the safety requirement
stated: a reused image is only safe with a **fresh container per pass** (what the
harness does) or a **warm container with a wiped workspace and a fresh
interpreter per mutant**, because a container whose workspace survives between
mutants is a container where one mutant can see another's files.

If it is done later, the number to beat is 0.91 s/pass, and the mechanism already
exists: Phase 3's `engine/install.py` builds hashed, wheel-only derived images.

## Finding 2 — which mutants CI runs, and were the 300 run on current code

**The sample is fixed, seeded and balanced.** `scripts/sandbox_gate.py` gained
`--seed` (default 7) and `_stratified()`, which shuffles the planned mutants with
the seed, takes half from each verdict and appends one canary of each verdict if
the split did not already contain one. The size was 60 while a pass cost 15 s; it
is now **300**, because a pass costs 0.91 s (Finding 1) and the full sample costs
what 60 used to. For the smaller `--mutants 60 --seed 7` variant:

```
size 60   tests-killed 30   survived 30
operators: delete_stmt 16, int_plus 14, int_minus 11, return_none 8,
           compare_swap 7, bool_flip 2, arith_swap 1, slice_bound 1
```

All eight operator families are represented, both verdicts are represented 30/30,
and the sample is reproducible from the seed alone — the gate prints the seed and
the composition into its result JSON, and the composition is checked, not just
the count. The previous version took the first N in plan order, which was fixed
but arbitrary: it could have been 60 killed mutants and then "byte-identical"
would only ever have been checked against one kind of behaviour.

**Were the 300 run on current harness code?** The 300/300 result was measured at
`7cd2afd`. Two changes since then touch the sandbox path:

* the plan now carries `checkout` / `program_root` (a plan-bytes change; the
  observation stream is unchanged, and the case-set hash is unchanged because
  the plan is not part of it);
* `sandbox_env()` now **overrides** `PATH` instead of defaulting it (see the bug
  below). This is a real behaviour change in the container.

Because of the second one, the 300/300 result is quoted as belonging to
`7cd2afd`, and a fresh gate run is what the report cites for the current tree.
After the PATH fix and the removal of the two fixed waits (Finding 1), the full
**300-mutant gate runs on the current tree**: `300/300` verdict agreement,
`byte_identical: true`, **0.91 s/pass**, 139 killed / 161 survived — the whole
sample, in 13 minutes. `docs/results/sandbox-gate-20261120.json` is that run.
CI's default is now 300 rather than 60, because the cost of the full sample is
what 60 used to cost.

### The bug this run found: a Windows `PATH` inside a Linux container

The first local gate run failed with `workspace copy-in tar exited 127`. The
container log said it plainly:

```
: not found
engine-run: 1: sleep: not found
```

`sandbox_env()` copied `PATH` from the host when present and only defaulted it
when absent. On this host the base `PATH` is `C:\Windows\System32;...`, so the
container's readiness loop could not find `sleep`, the wrapper never reached
`"$@"`, and the copy-in exec exited 127 — which reads as a transport failure.
On the Linux CI runner the host `PATH` happens to be valid POSIX, which is why
every CI run before this one passed. The environment is now fixed:

```python
SANDBOX_PATH = "/usr/local/bin:/usr/bin:/bin"
env["PATH"] = SANDBOX_PATH   # override, never inherit
```

`tests/test_harness_sandbox_transport.py::test_the_container_path_is_never_inherited_from_the_host`
pins it with a Windows-shaped input. A container is always Linux; nothing about
its environment may be inherited from the host that started it.

---

## Finding 3 — 137 is not a verdict, it is a question

`SandboxResult` now carries `oom_counter: int | None`, read from
`/sys/fs/cgroup/memory.events` **while the container is still held open**, and
`_death_observation()` classifies by evidence rather than by number:

| signal | `oom_kill` | verdict | reason |
| --- | --- | --- | --- |
| SIGKILL | `> 0` | `ResourceKill` / `unsupported` | the cgroup killed it |
| SIGKILL | `0` (readable) | `ProcessSignal` / `exception` | the process killed itself |
| SIGKILL | `None` (no cgroup) | `ResourceKill` / `unsupported` | no evidence; the conservative reading |
| SIGXCPU | any | `CpuBudget` / `unsupported` | the CPU rlimit fired |
| none | any | `ProcessExit` / `exception` | the target exited |

`None` is deliberately distinct from `0`: "I could not read the counter" is not
"the counter is zero", and the fallback is the reading that cannot turn a
resource kill into a target verdict.

`__process__` records `oom_counter` next to the exit code, so a reader can audit
the classification instead of trusting it.

New test, daemon-gated: `tests/test_harness_sandbox_hostile.py::test_a_target_that_sigkills_itself_is_a_behaviour_not_a_resource_kill`
runs a target that does `os.kill(os.getpid(), SIGKILL)` and asserts `exception`,
`ProcessSignal`, `SIGKILL`, `oom_counter == 0`. Daemon-free:
`tests/test_harness_crash_and_limits.py::test_sigkill_is_decided_by_the_cgroup_evidence_not_by_the_number`
walks all six rows of the table above.

Note that this ladder is why `137` appears twice with opposite meanings and both
are correct: in the hostile job the CPU-killed payload is `SIGXCPU` inside the
container (`152` as reported by the shell wrapper) and the OOM path is SIGKILL
with a non-zero counter.

---

## Finding 4 — the three new skips

CI skipped 28 tests, up from 25. The three new ones are the three tests in
`tests/test_harness_sandbox_hostile.py`, all gated on
`ENGINE_SANDBOX_TESTS=1`:

| test | why it is gated |
| --- | --- |
| `test_the_hostile_target_is_contained_and_the_attempts_are_recorded` | needs a real daemon: read-only rootfs, `network_mode=none`, `pids_limit` |
| `test_a_payload_killed_by_its_budget_still_returns_its_observations` | needs a real cgroup v2 CPU rlimit |
| `test_a_target_that_sigkills_itself_is_a_behaviour_not_a_resource_kill` | needs the container cgroup to read `memory.events` |

They are new *tests*, not new skips: nothing that used to run stopped running.
Final verified run: **`0c469bc`**, both jobs green.

* `ci` — **402 collected / 373 passed / 29 skipped / 0 failed**
* `sandbox-hostile` — **28 collected / 28 passed / 0 skipped / 0 failed**,
  gate `60/60, byte-identical True, 15.07 s/pass`, oracle canaries and the
  case-set freshness check both green.

The 29 skips break down as:

| file | count | gate |
| --- | --- | --- |
| `tests/test_sandbox_hostile.py` | 18 | `ENGINE_SANDBOX_TESTS=1` |
| `tests/test_install_hostile.py` | 7 | `ENGINE_SANDBOX_TESTS=1` |
| `tests/test_harness_sandbox_hostile.py` | 3 | `ENGINE_SANDBOX_TESTS=1` |
| one mutator test | 1 | needs `.harness/mutation/plan.json`, which CI does not produce |

All 28 docker-gated ones run in the `sandbox-hostile` job, which reports
**28/28 passed, 0 skipped** — the counts do not line up because the hostile job
runs a subset
(`tests/test_sandbox_hostile.py`, `tests/test_install_hostile.py`,
`tests/test_harness_sandbox_hostile.py`, `tests/test_harness_sandbox_transport.py`)
and the `ci` job runs everything else. A test that skips in one job and passes in
another is visible in exactly that pair of numbers.

Locally (Windows, no `ENGINE_SANDBOX_TESTS`) the run is **368 passed, 33
skipped, 0 failed**: the 29 above plus platform skips (`RLIMIT_CPU` twice,
`SIGSEGV`, `SIGKILL`-by-number, `SIGTERM`) that pass on the Linux runner. The
counts are not expected to match — a test that skips in one job and passes in
another is exactly what the pair is for.

---

## Finding 5 — how many of the 200 forks were actually created

**62.** The hostile target now records `forks_created` and the harness reads it
out of the dead container as a value:

```json
{"fork": "BlockingIOError", "forks_created": 62,
 "net": "OSError", "workdir": "/workspace", "write": "OSError"}
```

So `pids_limit` stopped the fork storm at 62 children, and the *refusal* is part
of the observation (`BlockingIOError`) rather than a harness error. The run
carries all four attempts — write outside `/workspace`, network connect, fork
without limit, and a `getcwd()` probe — and all four are recorded:

* `write`: `OSError` (read-only rootfs, and no host mount to reach)
* `net`: `OSError` (`network_mode=none`: no interface, no DNS)
* `fork`: `BlockingIOError` after 62 children (`pids_limit`)
* `workdir`: `/workspace` — the case never saw a host path

The test asserts `0 < forks_created < 200` rather than a fixed number, because
the exact count depends on how many the cgroup admits before the limit, which is
a property of the daemon, not of the harness. An earlier version of this test
used children that exited immediately, so "200 forks" succeeded trivially — a
fork bomb whose children die is bounded by nothing. The children sleep now, which
is what makes the limit the thing being measured.

---

## Finding 6 — the pycodestyle oracle is alive, and the sdist cannot be used

A second oracle is only worth having if it can be shown to be alive, so
`scripts/oracle_canary.py` asserts the two things that make an upstream suite
usable, for every pinned target, and writes `docs/results/oracle-canary-20261120.json`:

| target | unmutated tree | a known-broken mutant |
| --- | --- | --- |
| `parse` 1.22.4 | passes — `109 passed, 1 skipped in 1.62s` | `compare_swap@71:11` killed — `1 failed, 2 passed in 1.99s` |
| `pycodestyle` 2.15.0 | passes — `763 passed, 12 skipped in 5.31s` | `compare_swap@70:7` killed — `1 failed, 3 passed in 2.51s` |

A suite that is red on the unmutated tree "kills" every mutant and inflates the
rate; a suite that cannot fail deflates it. Both are now checked before any
mutation number is quoted, and the script exits non-zero if either check fails.

### Both artifacts are pinned, and the module is proved identical

`scripts/pin_oracle.py` downloads both tarballs, checks each against its pinned
sha256, and compares the module file byte for byte:

| artifact | sha256 | `pycodestyle.py` |
| --- | --- | --- |
| PyPI sdist (module under test) | `318f5db0…b50e` | `04d95f31…55fe` |
| GitHub tag tarball (runnable suite) | `3cb6afba…b4bd` | `04d95f31…55fe` |

`module_identical: true` — the oracle tree and the sdist ship the same module, so
the suite is an oracle for the code under test rather than for a near neighbour.
`harness/oracle/pycodestyle.json` is the record, regenerated by the script, and
the script exits non-zero if either hash or the module comparison changes. It runs
in CI next to the canaries.

### The sdist trap

`pycodestyle` 2.15.0's **PyPI sdist ships `tests/` but not `testing/`**, and
every test module imports `from testing.support import ROOT`. Running the sdist
suite fails collection outright:

```
ModuleNotFoundError: No module named 'testing'   (8 collection errors)
```

The pinned GitHub tree at the release tag (`codeload.github.com/PyCQA/pycodestyle/tar.gz/refs/tags/2.15.0`,
sha256 `3cb6afba…b5db4bd`) contains `testing/` and runs clean: **763 passed, 12
skipped**. So the stage-2 oracle uses the tag tarball, pinned by hash, and the
sdist stays pinned only as the *source of the module under test* — which is
exactly the split `scripts/fetch_target.py` already documents. Quoting the sdist
as the oracle would have produced "every mutant survived, the suite never ran",
with the same shape as the inverted-oracle bug from 12a: a number that looks
like a finding and is an infrastructure error.

### `--import-mode=importlib`

The suite needs it. In pytest's default `prepend` mode the tests directory goes
first on `sys.path`, and a mutant tree's `canary` / target module is shadowed by
whatever sits beside the tests — the same shadowing that made the 12a canary
report "survived" for a mutant that was not loaded at all. Two things about
this are worth stating:

* it changes `sys.path`, so a suite that depends on the old path layout could
  break. The `pycodestyle` suite does not: 763 pass in both modes, and the oracle
  canary passes with it enabled.
* it is the reason the canary exists. Without it, a mutant module that is silently
  not imported reads as a survivor, which is the single most expensive kind of
  wrong number in a mutation report.

---

## The lesson CI added: `sorted(Path)` is not portable

Two failures came out of the first green-attempt runs, and both are worth keeping:

* `os.listdir()` order was assumed in a test that checks the empty-`HOME` control.
  NTFS and overlayfs happened to return creation order; ext4 on the runner did
  not. Sorted now — a control check that is itself order-dependent is a control
  you cannot cite.
* `sorted(data.glob("*.py"))` sorts `Path` objects, and `PurePath.__lt__` is
  **case-normalised on Windows and case-sensitive on POSIX**. The stage-2 case
  file was built here in one order and rebuilt on the runner in another, giving a
  different case-set hash for the same 61 cases; the freshness check caught it,
  which is why the check exists. The key is `item.name` now, and `--check` passes
  in a Linux container as well as here.

Neither was caught locally because the local platform is one of the two. That is
the argument for a freshness check paid in CI rather than a comment asking people
to regenerate.

## What this does not answer

The stage-2 deliverables — CLI observation layer end to end, approvals loader,
case set, subprocess coverage, A/A baseline, the migration itself, `verify_change`
on it, stage-2 mutation — are not covered by this document. They are reported
separately: `docs/results/stage2-cli-20261120.md`.
