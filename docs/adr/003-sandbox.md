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
  environment** — it only runs pip and the result is committed as a derived
  image tagged `sandbox-deps-<sha256(requirements)>`. The derived image's
  digest is recorded with the run.
- **Run phase**: `network_mode='none'` from the derived image;
  `PATH=/opt/deps/bin:$PATH`, `PYTHONPATH=/opt/deps`.

**Install-phase hardening** (decided in checkpoint 7 review, **not yet
implemented** — no install-phase code exists yet):

- `pip install --only-binary :all:` wherever the pin set permits wheels:
  `--only-binary` refuses the `setup.py` execution path entirely, so the
  most dangerous install-time code (arbitrary build scripts of a malicious
  sdist) never runs. Packages that only publish sdists fail the install and
  surface as a decision, not a surprise. `--require-hashes` pins every
  artifact to the hash recorded at pin time — a compromised index cannot
  substitute a different wheel under the same version.
- The install container inherits the run phase's hardening **except the
  network**: `read_only` rootfs (pip writes to `/opt/deps`, a tmpfs),
  non-root user, `cap_drop: ALL`, `no-new-privileges`, CPU/memory/pids
  limits. The one deliberate widening is `network_mode` — installs need
  the index. No secrets and no workspace are mounted, so a malicious build
  script can reach only the index and its own scratch space.
- **Base image pinned by digest** (`python@sha256:...`): the same immutability
  rule the run phase already uses; a mutable `latest` tag would let an
  attacker's push silently change every future derived image.
- **The poisoning risk, stated plainly**: the derived image is keyed by
  `sha256(requirements)`, so a *poisoned build is reused by every run with
  the same requirements* — an attacker who once gets malicious code into
  the install phase (e.g. a typosquatted package whose wheel ships a
  modified `sitecustomize.py`, or an index-serving a backdoored artifact
  that still satisfies `--require-hashes` only if the pin itself was
  compromised) bakes it into the cache, and later runs never re-execute
  pip. Mitigations, in order: the pin file itself is workflow input and
  goes through review; hashes make silent artifact substitution detectable;
  the derived-image digest is recorded with every run, so an incident is a
  `grep` over run records, not a mystery; and a cache-bust (bump the pin,
  or purge `sandbox-deps-*` images) is a mechanical response. What
  content-addressing deliberately does NOT do is make poisoning
  impossible — it makes it *persist*, which is why it must be named here.
- The derived image is built with `container.commit()`, which preserves the
  container's filesystem but not its tmpfs mounts — `/opt/deps` must be a
  real layer directory, created as root then chowned, not a tmpfs.

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
paths, `..`, NUL, rejects duplicate regular-file entries, caps the entry
count at `copy_out_max_entries` = 20 000 and checks each file's declared
size *before* reading it — huge/sparse headers die in the header, not in
`extractfile` — and never materializes symlinks/hardlinks parses the
copy-out). There are therefore **no host mounts at all** — the
symlink-through-workspace door is closed at the boundary, and Windows'
slow, differently-permissioned `/mnt/c` bind mounts never appear.

`exec` stays allowed on the workspace (running untrusted code is the
feature; the boundary is the container), `nosuid`/`nodev` do not compromise
that.

**tmpfs pages are charged to the container's memory cgroup — measured, not
assumed.** tmpfs is shmem; the pages a fill faults in count against
`mem_limit` exactly like heap. Two hostile cases pin the relationship on
this daemon (Docker 28.3.2):

- *Inside the budget*: `workspace_mb=64` filled with 8 KiB files →
  `FILL-STOPPED errno 28` (ENOSPC) after 8192 files, payload exits 0,
  runner responsive. The size cap fires first.
- *Crossing the budget*: `workspace_mb=248, mem_mb=256` filled the same way
  → the kernel **OOM-killed the container before ENOSPC could fire**
  (exit 137, cgroup `oom_kill` counter > 0). tmpfs + interpreter overhead
  crossed the 256 MiB line while filling.

So the operating rule is: `workspace_mb + tmp_mb + interpreter headroom
must stay inside mem_mb`, and a caller who sets the workspace near the
memory limit converts every full-workspace fill from a clean ENOSPC into a
kernel OOM kill. Both outcomes are contained — but they are different
failures, and the limits dataclass documents which one a given
configuration produces. `copy_out_max_entries` (20 000) belongs here too:
it caps the number of archive entries the runner accepts back, so a tar
bomb cannot turn the runner's parse step into the next resource problem.

Trade-off: a tmpfs workspace dies with the container, which is exactly why
D3 moves identity into the DB.

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
- **137 provenance rule** (revised after review: exit 137 is SIGKILL, and
  the payload can send itself SIGKILL — 137 alone is an inference, not
  evidence). Evidence, in priority order:
  1. **The cgroup's `oom_kill` counter is the ground truth** — read from
     `/sys/fs/cgroup/memory.events` via exec **while the container is still
     held open** (the marker path; cgroup v2, measured on this daemon:
     the container sees its own cgroup at `/sys/fs/cgroup`, private
     cgroupns, `memory.events` readable by uid 65534, and `memory.max`
     mirrors `mem_limit` exactly). `oom_killed = counter > 0`; a counter of
     0 **proves** the kernel did NOT kill — a 137 with counter 0 is the
     payload killing itself (e.g. `os.kill(os.getpid(), SIGKILL)`), and is
     reported as such.
  2. **Counter unreadable** (cgroup v1 daemons, exec refused, or the exec
     record already reaped): fall back to the daemon's `State.OOMKilled`
     flag — set only when the *main* process is OOM-killed, which under the
     supervisor pattern the payload never is — and otherwise label a 137
     **"SIGKILL, probably OOM"**. This label is honest about being an
     inference.
  3. **No marker** (container exited, deadline, crash): the container is
     gone and there is nothing to exec, so only the daemon flag applies —
     and 137 alone no longer implies OOM.
  Known residual: on a cgroup-v1 daemon a *child* OOM leaves neither
  counter nor daemon flag, and the "probably OOM" label is the best
  available answer. Cgroup v2 is what this daemon and CI run.
  4. **Runner deadline** kills with SIGKILL at expiry — that is
     `timed_out=True`, never `oom_killed`; a container killed by something
     outside the runner (host OOM at the daemon level, operator `docker
     kill`) surfaces as exit-without-marker or an exception, not as OOM.
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

### D8 — The host-side workspace is an in-memory tree; tools never touch a host FS (checkpoint 8)

**Decision**: between sandbox runs, the workspace is a `WorkspaceTree` — a
mapping of validated path to bytes, **regular files only** — built once from
the content-addressed base snapshot by the SAME safe extractor that parses
copy-out (`sandbox.parse_workspace_tar`). Tools (`read_file`, `search`,
`apply_patch`) operate on this map; nothing in the tools module opens a
host path, follows a link, or shells out. The symlink door stays closed on
the host side *by construction* — there is no filesystem to escape into —
and the extractor's hostile-suite guarantees (absolute/`..`/NUL refusal,
duplicate-entry rejection, header-size caps, links never materialized)
apply to snapshot loads for free because it is one shared code path.

**`state_hash`** = sha256 of the sorted `(path \t sha256(content) \n)`
manifest — a pure function of the tree, so any worker that rebuilds
base+log computes the same token. It is the patch log's CAS currency and
`apply_patch`'s precondition.

**Caps** mirror the sandbox copy-out boundary (what can leave a sandbox can
enter a workspace, and no further): `FILE_MAX_BYTES` 4 MiB per file,
`MAX_FILES` 20 000, `SNAPSHOT_MAX_BYTES` 32 MiB, `PATCH_MAX_BYTES` 8 MiB,
`MAX_EDITS_PER_CALL` 64.

**Why in-memory rather than a temp directory**: a temp dir re-opens every
door the tree closes — path traversal against a real filesystem, symlink
smuggling, TOCTOU between validation and use, and cleanup liability. A
dict of bytes has no such surface, and workspaces in this phase are far
below the caps above (32 MiB is a bounded, copy-out-shaped amount to hold).

**search is literal matching, on purpose**: `str.find` has no backtracking,
so there is no ReDoS shape — no pattern a model supplies can pin the worker
CPU or starve the fenced heartbeat thread. A `re` pattern in the worker
process could hang it (catastrophic backtracking with the GIL held);
RE2 would fix that but adds a C-extension dependency to the runtime image;
regex-in-sandbox costs a container spin-up per search. Linear-time literal
matching covers "find this exact string" — the operation the run phase
actually needs — and the model still gets file+line excerpts.

**Path rules** (`paths.py`): no absolute paths, no `..` (and no `.` — no
normalization in the tree, so `a/./b` would be a distinct key for the same
file), no NUL, no empty/over-long paths (>1024 bytes total, >255 per
component), and no component equal to `.git` case-insensitively (the
workspace never carries repository metadata the model could rewrite).
**Backslashes are rejected outright** — the tree is POSIX-shaped, and a
backslash in model output is a Windows-path confusion attack, not a rare
legal filename. **No Unicode normalization**: Linux filenames are bytes;
NFC/NFD spellings are DIFFERENT files and must not be silently merged.
Paths decode as strict UTF-8 and compare byte-exactly. **Control
characters (Unicode Cc: newline, tab, CR, DEL) and bidirectional
overrides (U+202A–202E, U+2066–2069) are refused** — a newline breaks
line-oriented logs, and a bidi override reorders the DISPLAYED text so a
diff-review screen can show `report.txt.exe` while the real name ends in
`.exe`: the filename is an attack surface for human review, not only for
the tree. (Checkpoint 8 review round 1.)

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

- Sandbox tests run in TWO CI jobs: the `ci` job exercises the runner
  logic with stubs (38 tests) and skips the hostile cases unless
  `ENGINE_SANDBOX_TESTS=1` is set; the `sandbox-hostile` job runs the
  real-Docker suite on GitHub's Docker-equipped `ubuntu-latest` runners
  (18 cases, all passing on Docker 28.3.2 locally and in CI). The docker
  SDK is deliberately absent from the dev extras so the stub suite keeps
  proving the runner is optional; the hostile job installs it explicitly.
- The patch-log workspace and typed tools (checkpoint 8) implement on top
  of D3/D4; `apply_patch`'s precondition hash is the concurrency gate.
- The privilege problem is a standing rule: nothing that talks to jobs or
  models ever receives the Docker API client.

### Future work — garbage collection (checkpoint 8 review)

Orphan blobs (from rolled-back advance transactions are rolled back with
them, so what remains are blobs uploaded but never referenced by a
committed patch row, and the blobs of workspaces whose workflows reached a
terminal state) are never deleted today. The rule, mirroring the ADR 002
retention rule: **never delete anything a non-terminal workflow
references** — a GC pass may collect (a) blobs unreferenced by any
`workspaces`/`workspace_patches` row for N days, and (b) workspaces (and
their blobs, cascade) whose workflow is `succeeded`/`failed` and older
than the retention window. Rebuild traffic makes (b) safe: a terminal
workflow is never retried. Not implemented in checkpoint 8; tracked here
so it is a decision to make, not a thing to rediscover.
