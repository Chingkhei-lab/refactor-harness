# ADR 003: Sandbox runner and workspace for untrusted code

Status: accepted (checkpoint 7)
Date: 2026-11-16

## Context

Phase 3 lets the engine run untrusted code — repository code a user asked us
to analyze, and model-generated code. Docker is the boundary. It is
hardened (see the argument table in the design doc) but it is **not a
hardened multi-tenant boundary**: the container shares the host kernel, and
whatever process controls the Docker API is effectively root on the host.
gVisor or Firecracker are stretch goals, recorded as such.

Two facts shape every decision here:

1. **The Docker API is root on the host.** A container that can reach the
   Docker socket can mount the host's filesystem and become root. Therefore:
   the Docker socket is never mounted into any container, and no model, no
   tool, and no job payload ever passes raw Docker options. Tools see
   `run(spec)` with numeric limits; the hardening flags are constants of the
   runner (`src/engine/sandbox.py::create_kwargs`), pinned by a unit test.
2. **The workspace is the two-way door.** Whatever the sandbox touches must
   never be read by a privileged host process without hostile-input
   handling: planted symlinks/hardlinks make a following host tool read
   outside the workspace, and `git` on an untrusted tree executes hooks and
   config (e.g. `core.fsmonitor`). Decision D4 removes the shared path
   entirely; the tool layer (checkpoint 8) adds path validation for the
   host-side reads that remain.

## Decisions

### D1 — Runner lives in the engine; the SDK stays behind an adapter

`src/engine/sandbox.py` drives Docker through duck-typed protocols
(`SandboxClient` / `SandboxContainer`) plus `DockerSandboxAdapter`. The
engine never imports the docker package; the caller supplies the client.
This keeps mypy strict clean and makes the runner testable with stubs —
which is where the timeout/kill/removal-failure paths are exercised in CI.

### D2 — Dependencies: two phases, install networked, run offline

The run phase is `network_mode='none'`, but tests need installed packages.
Resolution:

- **Install phase** (per requirements hash, cached): a container from the
  pinned base image, network allowed, **no secrets, no workspace, no
  environment** — it only runs `pip install --target /opt/deps -r req.txt`
  and the result is committed as a derived image tagged
  `sandbox-deps-<sha256(requirements)>`. The derived image's digest is
  recorded with the run.
- **Run phase**: `network_mode='none'` from the derived image;
  `PATH=/opt/deps/bin:$PATH`, `PYTHONPATH=/opt/deps`.

Rejected alternative: downloading a wheel cache at run time with
`pip --no-index` — it still runs pip inside the sandbox (parse/rendering of
untrusted package metadata) and couples run-time behavior to cache state.
Baking the derived image moves all of that behind the install-phase
boundary, and the hash key makes it a pure cache.

### D3 — Workspace storage: content-addressed base snapshot + ordered patch log

The workspace must be continuable by ANY worker (step 1 on worker A, step 2
on worker B), so its identity lives in the workflow's state, never on a
worker. Three options were weighed:

- **Shared volume**: any container can read it, but it is host-side storage
  outside the runner's control — the symlink/hardlink door stays open, and
  nothing ties its lifetime to the workflow's terminal state.
- **Blob snapshots** (my earlier design): content-addressed blobs
  (`key = sha256(content)`) fix the stale-worker race — uploads never
  overwrite each other, a rolled-back attempt leaves a harmless orphan —
  but each step would re-upload the whole workspace, O(steps x size), and
  there is no audit trail of *what changed*.
- **Base + ordered patch log** (chosen): the workspace is a base snapshot
  (content-addressed, produced once by the clone step) plus an ordered log
  of content-addressed patches. Each step appends its patch; the advance
  transaction commits the reference (log index + tip hash) with the step
  output — the same all-or-nothing commit that guards step outputs.

Why the patch log wins:

- **Cost**: each delta is O(patch), not O(workspace).
- **Auditability**: the log IS the history; evals (Phase 6) can replay it.
- **Concurrency by construction**: a stale worker's patch carries the
  precondition hash of the file it rewrote; if the real attempt already
  moved on, the precondition does not match and the patch is rejected —
  no last-writer-wins, no hash-mismatched blob.
- **At-least-once delivery**: applying the same patch twice is rejected by
  the same precondition hash (a duplicate or stale apply is refused, not
  re-applied) — `apply_patch` is atomic: all hunks or none (checkpoint 8).
- **Rebuildability**: any worker checks out the base commit and applies the
  ordered log; workers are caches, the DB is the truth.

Orphan objects (from rolled-back or never-committed attempts) are harmless
because content-addressing makes them immutable and unreferenced; a GC
pass deletes objects unreferenced for N days, mirroring the ADR 002
retention rule (never free anything a non-terminal workflow references).

### D4 — Disk quota: size-capped tmpfs workspace inside the container

`fsize` caps one file but a bind-mounted workspace has no total quota, so
sandboxed code could fill the host disk, and the bind mount keeps the
host-side symlink door open. Chosen: the workspace is a **tmpfs inside the
container** (`size=64m,nosuid,nodev`), populated by copy-in and drained by
copy-out through the exec'd tar streams of D7 (the daemon archive API
cannot see tmpfs contents, and a safe extractor that refuses absolute
paths, `..`, NUL, and never materializes symlinks/hardlinks parses the
copy-out). There are therefore **no host mounts at all** — the
symlink-through-workspace door is closed at the boundary, and Windows'
slow, differently-permissioned `/mnt/c` bind mounts never appear.

`exec` stays allowed on the workspace (running untrusted code is the
feature; the boundary is the container), `nosuid`/`nodev` do not compromise
that. tmpfs pages are charged to the container's memory cgroup, so
`workspace_mb + tmp_mb` must stay inside `mem_mb` — the limits dataclass
documents this. Trade-off: a tmpfs workspace dies with the container, which
is exactly why D3 moves identity into the DB.

### D5 — Timeout, kill, and cleanup semantics

- The wall-clock deadline is enforced by the **runner**, never by anything
  inside the container (the timed process cannot be trusted to time
  itself). The wait is a trichotomy (`_wait_for_done`): the supervisor
  marker (payload finished — container still held open, copy-out now),
  container exit without a marker, or deadline expiry.
- On expiry: SIGKILL, then a short re-wait, then removal **in a finally**
  with bounded retries (`force=True`). A D-state process survives SIGKILL
  until its I/O returns, so force-remove may hang: after 3 attempts the run
  is recorded as `kill_failed` and the container is left for the janitor —
  removal failure is an expected, measured outcome, never an exception.
  A timed-out run's workspace mutations are discarded with the container;
  the patch log advances only on runs that reported completion.
- **Janitor**: a periodic pass removes labeled containers older than N
  minutes (`sweep_containers`), so a SIGKILL'd worker (the Phase 1 chaos
  scenario) cannot leak containers.
- Output caps: logs are streamed and capped per stream (1 MiB default,
  `truncated` flags); the daemon-side log driver is capped too
  (`json-file max-size=10m`) so an endless printer cannot fill host disk.
- `container.wait(timeout=...)` raises a timeout-type exception that
  differs by SDK version; requests' `ReadTimeout`/`ConnectionError` and
  docker API errors all subclass `OSError`, so the wait loop catches
  `OSError` and re-checks the deadline. Each wait slice is capped by the
  remaining wall time, never a constant.
- On retry, any container with the same name is removed before create.
- Infrastructure failures (create/start/copy-in/copy-out) raise
  `SandboxError` — and leave no container: the `finally` tears down on any
  pre-completion failure path.

### D6 — Where the worker runs

The worker (and the hostile suite) runs **host Python against Docker
Desktop's WSL2 backend**. The workspace travels through the Docker API as
tar streams, never as a `/mnt/c` bind mount, so cross-OS path/permission
problems do not arise. WSL2-native execution is the noted alternative if
API-throughput ever matters.

### D7 — Workspace transport: exec'd tar streams + supervisor pattern (all measured)

Four behaviors of Docker 28.3.2 (API 1.51, Docker Desktop/WSL2, SDK 7.2.0)
contradict the naive archive-API design, each measured on this daemon:

| Measured behavior | Naive design it breaks | The runner instead |
| --- | --- | --- |
| The daemon **refuses archive PUTs into read-only containers** ("container rootfs is marked read-only") — both before and after start, even into the mounted tmpfs | `put_archive` after create | copy-in runs `tar -xf - -C /workspace` **inside** the container: the exec'd process shares the container's mount namespace, so the tmpfs is writable to it (the `kubectl cp` mechanism) |
| The daemon archive API **cannot see tmpfs contents**: `GET /workspace` returns only the mount-stub directory entry; single-file GETs 404 even for real files on a running container | copy-out via `get_archive` | copy-out runs `tar -cf -` **inside** the container; the runner demuxes and parses the stream |
| Windows npipe transport: `shutdown()` **aborts** the stream (tar exits 128, nothing extracted) | half-close to signal EOF | **no half-close**: a tar archive self-terminates on its zero-block trailer |
| Stream EOF arrives as `RuntimeError` / `pywintypes.error` (109 'ReadFile'), not an empty `recv` | `except OSError` | transport-tolerant `_DRAIN_ERRORS` tuple (`pywintypes` imported by name, guarded — the engine never imports docker) |
| The daemon **reaps finished exec instances** → `exec_inspect` can 404 | trust the inspect exit code | exit code unavailable ⇒ fallback verification: read `/workspace` back (directory GETs work) and require the ready-flag tar member |

**Lifecycle**: `create → start → copy_in (exec tar) → wait for marker /
exit / deadline → copy_out (exec tar) → remove ×3`. Copy-in raises
`SandboxError` on any failure and the `finally` tears the container down —
copy-in is infrastructure, never a run outcome.

**Supervisor pattern.** With a workspace, the payload cannot be the main
process twice over: (1) untrusted code must not start before copy-in
lands; (2) the tmpfs dies with the container, so the container must stay
up after the payload exits, until copy-out. `command_for` therefore wraps
the argv in a **fixed runner script** — the user argv reaches it only as
positional parameters via `"$@"`, never interpolated into the shell
string, so there is nothing to inject:

```sh
while [ ! -e /workspace/.engine_ready ]; do sleep 0.05; done
"$@"                                          # the user argv, as-is
status=$?; echo "ENGINE_DONE_${ENGINE_DONE_NONCE}=${status}"
exec sleep 31536000                           # hold the container for copy-out
```

The nonce is per-run (`secrets.token_hex(16)`), injected via
`ENGINE_DONE_NONCE`; the log reader scans stdout frames for
`ENGINE_DONE_<nonce>=<code>` (carrying a buffer across frame splits) and
the code becomes `RunResult.exit_code`. Without a workspace there is no
wrapper: the argv list is the command and the container's exit code is the
answer.

Marker semantics:

- **Exit-code authority**: the marker when present, else `State.ExitCode`.
- **OOM rule**: a marker exit of 137 means OOM. The payload is a *child*,
  so the daemon's `State.OOMKilled` can stay `False` on a child OOM; 137 is
  the kernel OOM killer's signature and the only in-container SIGKILL
  source (the runner kills only at the deadline, after which no marker
  exists).
- **Copy-out timing**: copy-out runs only when the payload reported
  completion through the marker — a timed-out run's mutations die with the
  container (the workflow retry rebuilds from the patch log). The
  supervisor waits for the child to exit before echoing, so in the honest
  path the workspace is quiescent when copied out.
- **`.engine_ready` is never removed** by the wrapper: removing it races
  the runner's verification. It stays for the run's lifetime; copy-out
  filters engine-owned names (`.engine_*`) from the results.
- **Forgeable marker (documented threat)**: the payload owns its stdout, so
  it can print a fake `ENGINE_DONE_<nonce>=0` and keep running, mutating
  the tmpfs during copy-out (a torn snapshot). This is **self-sabotage** —
  equivalent to writing garbage files and then exiting 0, which nothing
  prevents; the runner's walls (deadline, caps, teardown) do not depend on
  the marker's honesty. The damage is bounded downstream: copy-out
  materializes regular files only, and the patch log (D3) applies
  content-addressed patches under a precondition hash, so a torn patch is
  rejected at apply time, not silently merged. Accepted for checkpoint 7;
  re-evaluate if evals (Phase 6) need a stronger quiescence guarantee.

## The exact container-create arguments

The hardening profile (`create_kwargs`), pinned by
`tests/test_sandbox.py::test_create_kwargs_are_hardened`:

| Argument | Value | Purpose |
| --- | --- | --- |
| `image` | pinned by digest (`python@sha256:...`) | immutable base |
| `command` | argv list; with a workspace, the fixed supervisor wrapper + argv via `"$@"` | no injection; gate + hold-open for copy-out |
| `name` | `engine-sandbox-{job}-{attempt}-{run_key}` | janitor + retry |
| `network_mode` | `none` | no interfaces, no DNS |
| `user` | `65534:65534` | non-root |
| `working_dir` | `/workspace` | |
| `read_only` | `True` | immutable root filesystem |
| `tmpfs` | `/tmp: size=16m,noexec,nosuid,nodev`; `/workspace: size=64m,nosuid,nodev,uid=65534,gid=65534,mode=0755` | disk quota + scratch |
| `environment` | allowlist dict + `ENGINE_DONE_NONCE` (supervisor marker nonce) | no host secrets |
| `cap_drop` | `["ALL"]` | no capabilities |
| `security_opt` | `["no-new-privileges"]` | no suid escalation |
| `nano_cpus` | `limits.cpus * 1e9` | CPU quota |
| `mem_limit` | `limits.mem_mb * MiB` | memory |
| `memswap_limit` | same as `mem_limit` | no swap |
| `pids_limit` | `limits.pids` | fork bomb containment |
| `ulimits` | `nofile` 256/256, `fsize` 32 MiB/32 MiB | fd + per-file caps |
| `log_config` | `json-file`, `max-size=10m, max-file=1` | daemon-side log cap |
| `labels` | `engine-sandbox`, `.created`, `.job`, `.attempt` | janitor + tracing |
| `stdin_open`/`tty` | `False`/`False` | demuxed stream protocol |
| `auto_remove` | `False` | runner reads logs/attrs, removes itself |

Deliberately absent: `privileged`, any `volumes`/`binds`, `devices`,
`pid_mode`, `ipc_mode`, and every raw option a caller could pass. The
seccomp profile is Docker's default: measured on this daemon,
`io_uring_setup` (syscall 425) is refused with `EPERM` (errno 1) — recorded
as a hostile-suite observation, not an assumption.

## Consequences

- Sandbox tests need Docker and therefore run on the dev machine, not in
  CI; CI exercises the runner logic with stubs (30 tests) and skips the
  hostile cases unless `ENGINE_SANDBOX_TESTS=1` is set (14 real-Docker
  cases, all passing on Docker 28.3.2).
- The patch-log workspace and typed tools (checkpoint 8) implement on top
  of D3/D4; `apply_patch`'s precondition hash is the concurrency gate.
- The privilege problem is a standing rule: nothing that talks to jobs or
  models ever receives the Docker API client.
