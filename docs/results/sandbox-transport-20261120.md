# The harness inside the sandbox: gate, hostile target, overhead

Evidence, not proof: this says the containerised path produced **the same
verdicts and the same bytes** as the direct path on 300 mutants of one target. It
does not say the sandbox is safe against every hostile program, and the overhead
figure is for this host and this image.

## What changed

A pass used to be a host subprocess: `python -m harness.child` started with
`subprocess.Popen` in the checkout directory, no container, no network isolation,
no limits. Mutation, refactor and verification all went through it. That
contradicted ADR 003's claim that untrusted code runs in the sandbox, so
`runner.run_pass(executor=...)` now has a transport.

The transport follows ADR 003 D4 — **no host mounts**:

- the workspace (checkout, determinism shim, harness sources, plan) is built as
  bytes and **copied in as a tar stream** into the container's size-capped tmpfs
  at `/workspace`;
- the child runs there under the unchanged Phase 3 profile: `network_mode=none`,
  read-only root filesystem, `cap_drop=ALL`, `no-new-privileges`, non-root
  `65534`, cgroup limits on memory, pids, CPU and file descriptors;
- observations are **copied out as a tar stream** after the run.

## Observations from a dead payload

The supervisor runs the payload as a *child* and prints its exit code on a nonced
marker line before holding the container open for the copy-out. So a payload
killed by its `RLIMIT_CPU` does not take the container with it:

1. the child sets `RLIMIT_CPU` from the plan (soft = used + budget, hard = the
   pass-wide backstop) **inside** the container;
2. the kernel kills it with SIGXCPU;
3. the supervisor sees exit status 152 (shell convention, 128+24), prints the
   marker, and keeps the container alive;
4. the copy-out returns `out.jsonl` (every observation written so far) **and**
   `out.begin` (the begin marker naming the in-flight case);
5. the runner records that case `unsupported (CpuBudget)` and resumes.

A container-level wall-clock kill is different: the tmpfs dies with it and the
observations are lost, which is why the per-case bound is an in-container
`RLIMIT_CPU` and not a container kill. Both paths are tested
(`tests/test_harness_sandbox_hostile.py`, in the `sandbox-hostile` job).

The first CI run of this found a real bug: a shell reports `128+n`, the runner
only understood negative return codes, so a CPU kill inside the container was
being compared as "the target exited 152". Both conventions now classify
identically (`tests/test_harness_crash_and_limits.py`).

## The regression gate

`scripts/sandbox_gate.py` runs in the `sandbox-hostile` job and publishes its
summary as an annotation (job logs need admin rights on this repository):

| Result | Value |
| --- | --- |
| Mutants re-run through the container | 300 |
| Verdicts agreeing with the direct path | **300/300** |
| Observation stream for the 273-case set | **byte-identical** |
| Overhead per pass (fresh container, copy-in, copy-out, teardown) | **15.12 s** |
| The pass itself, same host | ~1.2 s |

15.12 s per pass is the honest cost of a fresh container per pass: ~1.2 s of work
inside, the rest is container create/start, the two tar streams and teardown. It
makes the 300-mutant gate a ~50-minute job, which is why CI runs it at a smaller
default size (`GATE_MUTANTS`, 60) and the full 300-mutant run is recorded here.
Paying it per *comparison* would be wasteful; paying it per *mutant* is the price
of not running untrusted code on the host.

## The hostile target

`tests/fixtures/canary/hostile_target.py` attempts three escapes in one case and
records what happened as an ordinary observation, so the assertion is on what the
target managed to do, not on how the harness felt:

| Attempt | Observed |
| --- | --- |
| write `/etc/harness-escape` (outside the workspace) | refused; recorded as an exception name |
| `socket.create_connection(("1.1.1.1", 53))` | refused (no interfaces, no DNS) |
| 200 sleeping children | refused at the cgroup `pids_limit` |
| `os.getcwd()` | `/workspace`, i.e. the tmpfs, not a host path |

The sandbox-hostile job reports **27 collected / 27 passed** with these included
(25 before, plus the containment case and the CPU-kill recovery case).

## What is not covered

- **Docker only.** No other runtime is exercised; `LocalExecutor` exists for tests
  and is explicitly not a boundary.
- **One image, one target.** `python:3.12-slim` and `parse` 1.22.4. A target with
  its own dependencies needs an image that has them, which is stage 2's problem.
- **Per-case overhead is the container, not the case.** A cheaper design (one
  container per pass instead of per case) was not attempted: the pass already runs
  in one child, and a pass *is* the unit of isolation.
