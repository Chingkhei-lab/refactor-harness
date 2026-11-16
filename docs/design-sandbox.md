# Sandbox design (Phase 3)

Status: checkpoint 7 design. Decisions: [`docs/adr/003-sandbox.md`](adr/003-sandbox.md).

## Threat model

**Assets to protect**

1. The **host** and its filesystem: no host path may be readable or
   writable by sandboxed code, directly or through a planted link.
2. **Secrets**: environment, credentials, the database URL. The sandbox gets
   an explicit allowlist; nothing is inherited.
3. **Other runs**: one job's sandbox must not observe or disturb another
   (no shared network, no shared writable storage, unique containers).
4. **The network**: outbound connections and DNS must fail.

**Adversary**: untrusted repository code (clone → run tests) and
model-generated code (typed tools in checkpoint 8). The adversary is
assumed to have full code execution *inside* the container — running
untrusted code is the product, so containment, not prevention, is the goal.

**Trust boundaries**

| Boundary | Trust relationship | Consequence |
| --- | --- | --- |
| container ↔ host kernel | shared kernel | a kernel 0-day reachable through an allowed syscall escapes the container; Docker is not a hardened multi-tenant boundary. gVisor (user-space kernel) or Firecracker (micro-VM) are stretch goals. |
| runner ↔ Docker API | whoever holds the API is root on the host | the API client is only ever held by the runner; never mounted into a container; no model/tool passes raw Docker options; all limits are runner config. |
| copy-out boundary | results are hostile input to the host side | the workspace travels back through an exec'd tar (the daemon archive API cannot see tmpfs contents — measured); the safe extractor materializes regular files only, refuses absolute paths/`..`/NUL, caps total size; symlinks are counted and skipped, never followed. |
| copy-in boundary | content produced by the runner itself | still validated by the same rules (contract, defense in depth); travels in through an exec'd tar because the daemon refuses archive PUTs into read-only containers (measured). |
| tool layer (checkpoint 8) | paths arrive from the adversary | resolve against the workspace root; reject absolute paths, `..`, NUL, and any resolved path that leaves the root; deny `.git` writes; limit read sizes; refuse binaries. |

**Hardening stack** (what stops what — the hostile suite measures each):

network_mode=none → outbound TCP/DNS. read-only rootfs + no-new-privileges
→ rootfs writes, suid escalation. cap_drop=ALL → mount, ptrace, net_admin.
pids_limit → fork bombs. mem_limit==memswap_limit → memory hogs (OOMKilled).
tmpfs size + fsize → disk fill. ulimit nofile → fd exhaustion. wall-clock
kill in the runner → infinite loops. output caps (+ log driver max-size) →
output floods. Default seccomp profile → e.g. io_uring_setup (measured, not
assumed — the Docker version decides).

**Privilege problem (standing rule).** The process controlling the Docker
API can mount `/`, read any host file, and create privileged containers.
Therefore the Docker API client is created only inside the runner's host
process; it is never passed to jobs, tools, models, or containers; and no
tool call ever carries Docker options.

## Where the worker runs

Windows host Python → Docker Desktop (WSL2 backend). The workspace moves
through the Docker API (tar streams), so no `/mnt/c` path ever appears.
Sandbox tests run from the host with `ENGINE_SANDBOX_TESTS=1`; CI runs the
stub-based runner tests only.

## Architecture

```
run(spec)                          (src/engine/sandbox.py)
  create(...)        ← hardening profile: ADR 003 table
  start()
  stream logs ──thread── demux → per-stream cap (1 MiB), truncated flags
                    └── scans stdout for ENGINE_DONE_<nonce>=<code>
  copy_in(...)      ← exec'd tar: sendall(workspace + .engine_ready),
                      drain, inspect exit (or verify flag via dir GET)
  wait loop ──────── marker | container exit | wall-clock deadline (slices
                      capped by remaining time); on expiry: SIGKILL → re-wait
  copy_out(...)     ← exec'd tar `tar -cf - -C /workspace .`: demux, safe
                      parser (regular files only; symlinks counted+skipped),
                      `.engine_*` filtered, copy_out_mb cap
  finally: remove(force=True) ×3   → kill_failed + janitor on failure
janitor: sweep_containers() — labeled containers older than N minutes
```

**Supervisor wrapper** (with a workspace; `command_for`): a fixed runner
script — wait for `.engine_ready` (arrives inside the copy-in tar), run the
user argv via `"$@"` (never interpolated), echo the nonced
`ENGINE_DONE_<nonce>=<code>` marker, then `exec sleep` to hold the container
open so copy-out happens before the tmpfs dies. Exit-code authority: the
marker, else `State.ExitCode`; a marker exit of 137 means OOM (the payload
is a child — the daemon's OOMKilled can stay False). Timed-out runs discard
workspace mutations; infrastructure failures (create/start/copy-in/copy-out)
raise `SandboxError` and tear the container down in `finally`.

**Measured Docker 28.3.2 constraints** (each encoded in the runner, ADR 003
D7): archive PUTs refused into read-only containers (before AND after
start, even into tmpfs); the archive API blind to tmpfs contents (dir GET →
mount stub only, single-file GET → 404); npipe `shutdown()` aborts the
stream (no half-close — the tar trailer ends it); EOF as
`RuntimeError`/`pywintypes.error` (`_DRAIN_ERRORS`); finished exec records
reaped → `exec_inspect` can 404 → flag-based fallback verification.

- **Two-phase dependencies** (ADR 003 D2): install phase networked/no
  secrets → derived image `sandbox-deps-<sha256(requirements)>`; run phase
  offline from that image.
- **Workspace** (ADR 003 D3): base content-addressed snapshot + ordered
  content-addressed patch log in the DB; identity in workflow state, never
  on a worker; orphan GC after N unreferenced days.
- **Failure semantics**: `RunResult` carries `exit_code`, `timed_out`,
  `oom_killed`, `kill_failed`, capped `stdout`/`stderr` with truncated
  flags, `files` (copied-out workspace, regular files only),
  `symlinks_skipped`, and duration. Run outcomes never raise.

## Hostile suite (checkpoint 7 scope)

Each case runs real code in a real container and asserts containment, that
the runner stays responsive, and that no container is left behind
(fixture-checked per test). Removal-failure and janitor paths are covered
by stub tests in CI plus a real janitor case. Tool-layer path tests
(traversal, huge/binary files, patch atomicity, precondition hash, `.git`
write denial) arrive with the tools in checkpoint 8.

**Measured on Docker 28.3.2 / API 1.51 (all 14 cases passing):**

| Attack | Observed containment | Limit that stopped it |
| --- | --- | --- |
| fork bomb (`os.fork` loop) | EAGAIN (errno 11) after 63 forks, runner responsive | `pids_limit=64` (pids cgroup) |
| infinite loop | killed at the wall: exit 137, `timed_out=True`, `kill_failed=False` | runner wall clock |
| memory hog | exit 137, `oom_killed=True` | `mem_limit` == `memswap_limit` |
| disk fill (write loop) | EFBIG (errno 27) before the tmpfs total is reached | `fsize` ulimit (32 MiB/file) |
| output flood | exactly 1 MiB captured, `truncated=True` | per-stream output cap |
| outbound TCP / DNS | connect fails / `gaierror`, duration ~1 s | `network_mode=none` |
| rootfs write + mkdir | EACCES (13) on root-owned files, EROFS (30) on creates | read-only rootfs (+ DAC) |
| host paths (`/mnt/c`, docker.sock) | absent; rootfs is `overlay` | no bind mounts, no socket |
| env canary in the allowlist | absent from container env | env allowlist |
| suid escalation | `NoNewPrivs=1` (base image ships suid binaries — neutralized); ICMP raw refused EPERM; ICMP dgram creatable but `sendto` refused EINVAL | `no-new-privileges` + `cap_drop=ALL` |
| io_uring_setup (syscall 425) | `ret=-1, errno=1 (EPERM)` — measured, not assumed | default seccomp profile |
| symlink through workspace | copy-out returns `{"real.txt": b"payload"}`, `symlinks_skipped=2` — links never materialized or followed | safe extractor |
| janitor | stale container removed, fresh kept, none left behind | `sweep_containers` |

## Report (checkpoint 9)

`docs/results/sandbox-<date>.md`: containment matrix (attack, expected,
observed, limit that stopped it), container start overhead p50/p95, and the
maximum concurrent sandboxes observed on 4 CPUs.
