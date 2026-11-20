"""Sandbox runner for untrusted code (Phase 3, checkpoint 7).

Threat model: docs/design-sandbox.md; decisions: docs/adr/003-sandbox.md.

The runner drives Docker through a duck-typed client (SandboxClient /
SandboxContainer protocols plus DockerSandboxAdapter, the only SDK-facing
glue) so the engine package never imports the docker SDK — the caller (tests,
the phase-3 worker) supplies a docker-py client. Every hardening flag is a
runner constant, not a parameter: callers configure NUMBERS (memory, CPUs,
pids, wall time, caps), never raw Docker options. Whatever process controls
the Docker API is effectively root on the host, so the API surface shown to
models/tools stays this runner's narrow spec, never the SDK.

Container lifecycle: create -> start -> copy_in (workspace tar via exec'd
tar — the daemon refuses archive PUTs into read-only containers) -> stream
logs with per-stream caps -> wait with wall-clock deadline -> kill on expiry
-> remove with retries; a container whose removal keeps failing is recorded
as kill_failed and left for the janitor (a D-state process only dies when its
I/O returns — force-remove may hang, so cleanup failure is an expected,
measured outcome, not an exception).

Copy-in happens AFTER start and via exec: measured on Docker 28.3.2, the
daemon refuses archive PUTs into read-only containers ("container rootfs is
marked read-only") even into the mounted tmpfs. When a workspace is copied
in, the container therefore runs a FIXED wrapper (command_for) that waits
for a ready flag inside the tar, so untrusted code never executes before
the copy-in lands; the user argv reaches the wrapper only through "$@" and
is never interpolated into a shell string.
"""

from __future__ import annotations

import io
import posixpath
import secrets
import tarfile
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

# How often the supervisor-marker event is checked while a payload runs, and how
# often the container's state is re-read when no marker has arrived. The marker
# arrives on the log reader's thread, so waiting on the event costs nothing and
# wakes instantly; `container.wait(timeout=...)` is a CLIENT-side request timeout
# (docker-py passes it to `_post(url, timeout=...)`), and the container is held
# open by the supervisor's sleep, so every call blocked for the full slice before
# the event was looked at. Measured: 5.0 s of dead time per pass, 10.0 s when the
# payload crossed a slice boundary.
_MARKER_POLL_SECONDS = 0.02
_LIVENESS_POLL_SECONDS = 0.5
_WAIT_SLICE_SECONDS = 5.0  # retained for callers that pass their own deadline
_REMOVE_RETRIES = 3
_REMOVE_BACKOFF_SECONDS = 0.5
_LOG_JOIN_TIMEOUT_SECONDS = 10.0
_COPY_OUT_MAX_BYTES = 32 * 1024 * 1024
# Label namespace for ALL engine-owned containers (sandbox runs AND install
# builds — one janitor sweeps both). Public so the install phase labels its
# containers into the same namespace.
LABEL_PREFIX = "engine-sandbox"
_LABEL_PREFIX = LABEL_PREFIX  # internal alias kept for readability below
_SANDBOX_USER = "65534:65534"  # nobody:nogroup in the pinned Debian images
_WORKSPACE_DIR = "/workspace"
_COPY_IN_DRAIN_TIMEOUT_SECONDS = 30.0
_EXEC_EXIT_POLL_SECONDS = 5.0
_COPY_OUT_MAX_ENTRIES = 20_000  # hostile-archive cap: tar entries accepted
# Stream-end signals vary by Docker transport: unix sockets raise OSError
# subclasses, the Windows named-pipe transport raises RuntimeError and
# pywintypes.error (pywin32, pulled in by the docker SDK on Windows —
# imported by NAME only and guarded, so non-Windows hosts never need it).
_DRAIN_ERRORS: tuple[type[BaseException], ...] = (OSError, RuntimeError)
try:  # pragma: no cover - exercised only on Windows named-pipe hosts
    import pywintypes

    _DRAIN_ERRORS = (*_DRAIN_ERRORS, pywintypes.error)
except ImportError:
    pass
_DONE_PREFIX = b"ENGINE_DONE_"  # nonce and exit code follow on the same line
_SUPERVISOR_HOLD_SECONDS = 31536000  # `exec sleep` keeps the container up
# for copy-out; the runner tears the container down when the run is over
# Copy-in gate: the supervisor waits for this flag, which arrives inside the
# tar stream (see copy_in). It is NOT removed afterwards — no race with the
# flag check — and copy-out filters engine-owned names.
_READY_FLAG = ".engine_ready"
_WRAPPER_SCRIPT = (
    f"while [ ! -e {posixpath.join(_WORKSPACE_DIR, _READY_FLAG)} ]; do "
    "sleep 0.05; done; "
    '"$@"; status=$?; '
    'echo "ENGINE_DONE_${ENGINE_DONE_NONCE}=${status}"; '
    f"exec sleep {_SUPERVISOR_HOLD_SECONDS}"
)

# Frame type codes of the Docker multiplexed stream protocol (tty=False):
# 0 = stdin (never sent this way), 1 = stdout, 2 = stderr.
_STDOUT_FRAME = 1
_STDERR_FRAME = 2


class SandboxError(RuntimeError):
    """The runner could not set up or take down the sandbox (infra failure)."""


class SandboxUnsafeArchive(ValueError):
    """An archive member is hostile (absolute path, .., NUL) or oversized."""


@dataclass(frozen=True)
class SandboxLimits:
    """The ONLY knobs a caller may tune — numbers, never raw Docker options.

    tmpfs pages are charged to the container's memory cgroup, so
    workspace_mb + tmp_mb must stay inside mem_mb — a fill that crosses
    mem_limit OOM-kills the container before ENOSPC can fire (measured);
    inside the budget the same fill fails cleanly with ENOSPC. copy_out_mb
    caps the runner-side memory for the copied-out workspace and
    copy_out_max_entries the number of tar entries accepted (hostile
    tar-bomb cap); neither is a container quota.
    """

    mem_mb: int = 256
    cpus: float = 1.0
    pids: int = 64
    wall_seconds: float = 30.0
    output_bytes_per_stream: int = 1024 * 1024
    workspace_mb: int = 64
    tmp_mb: int = 16
    fsize_mb: int = 32
    nofile: int = 256
    copy_out_mb: int = 32
    copy_out_max_entries: int = 20_000


@dataclass(frozen=True)
class SandboxSpec:
    """What to run. `env` IS the environment allowlist; nothing else passes."""

    image: str  # pinned by digest
    argv: tuple[str, ...]  # argv list — never a shell string
    env: dict[str, str] = field(default_factory=dict)
    workspace_files: dict[str, bytes] = field(default_factory=dict)
    run_key: str = ""  # unique per run; default: fresh uuid
    job_id: str = "adhoc"
    attempt: int = 0


@dataclass(frozen=True)
class RunResult:
    """Outcome of one sandbox run. Structured, never an exception for the run
    itself; only infrastructure failures (create/start/copy-in) raise
    SandboxError.

    With a workspace, `files` is the copied-out workspace (regular files
    only, symlinks never materialized) and `symlinks_skipped` counts what
    the safe extractor refused to follow. `files` is empty when there is no
    workspace or the run timed out (mutations are discarded — the workflow
    retries; the patch log advances only on a marker-successful run).
    """

    job_id: str
    attempt: int
    container_id: str | None
    exit_code: int
    timed_out: bool
    oom_killed: bool
    kill_failed: bool  # kill+remove cleanup failed; container left for janitor
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool
    files: dict[str, bytes]
    symlinks_skipped: int
    duration_seconds: float
    # The cgroup's own oom_kill counter, read while the container was held open.
    # None means "no cgroup evidence" (cgroup v1, exec refused, container gone) —
    # callers must not read None as zero.
    oom_counter: int | None = None


@dataclass(frozen=True)
class CopyOut:
    """Results copied out of a sandbox. Symlinks are never materialized."""

    files: dict[str, bytes]
    symlinks_skipped: int
    truncated: bool


@dataclass(frozen=True)
class SweepResult:
    """Janitor pass: orphans removed, removals that failed (retried later)."""

    removed: int
    failed: int


class SandboxContainer(Protocol):
    """Duck-typed slice of a docker-py Container (and of test stubs)."""

    attrs: dict[str, Any]
    id: str | None

    def start(self) -> None: ...

    def wait(self, timeout: float | None = None) -> Any: ...

    def kill(self, signal: str | None = None) -> None: ...

    def logs(
        self,
        *,
        stream: bool = ...,
        follow: bool = ...,
        stdout: bool = ...,
        stderr: bool = ...,
    ) -> Any: ...

    def reload(self) -> None: ...

    def get_archive(self, path: str) -> Any: ...

    def remove(self, *, force: bool = ...) -> None: ...


class SandboxClient(Protocol):
    """Duck-typed slice of docker-py's containers + low-level exec API.

    The exec methods power the workspace copy-in (copy_in): the daemon
    refuses archive PUTs into read-only containers, so the workspace travels
    as a tar stream piped into `tar` running inside the container — the same
    mechanism `kubectl cp` uses. Exec lives on the CLIENT in docker-py's
    low-level API, hence the container-id parameter.
    """

    def create(self, **kwargs: Any) -> SandboxContainer: ...

    def list_containers(self, filters: dict[str, Any]) -> list[SandboxContainer]: ...

    def exec_create(self, container_id: str, cmd: list[str]) -> Any: ...

    def exec_start(self, exec_id: str, *, socket: bool = ..., stream: bool = ...) -> Any: ...

    def exec_inspect(self, exec_id: str) -> Any: ...


class DockerSandboxAdapter:
    """Bridges a docker-py client to SandboxClient. The caller imports the
    docker SDK and hands the client in; this module never does."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def create(self, **kwargs: Any) -> Any:
        return self._client.containers.create(**kwargs)

    def list_containers(self, filters: dict[str, Any]) -> list[Any]:
        # docker-py takes `all` as its own argument, not a filter key
        real_filters = {k: v for k, v in filters.items() if k != "all"}
        return list(self._client.containers.list(all=True, filters=real_filters or None))

    def exec_create(self, container_id: str, cmd: list[str]) -> Any:
        return self._client.api.exec_create(container_id, cmd, stdin=True)

    def exec_start(self, exec_id: str, *, socket: bool = False, stream: bool = False) -> Any:
        return self._client.api.exec_start(exec_id, socket=socket, stream=stream)

    def exec_inspect(self, exec_id: str) -> Any:
        return self._client.api.exec_inspect(exec_id)

    def image_attrs(self, ref: str) -> dict[str, Any] | None:
        """docker image inspect, mapped to None when the image is absent.

        Broad catch on purpose: any failure to classify the image (missing,
        daemon hiccup mid-inspect) reads as "not cached", and the build path
        below surfaces the real daemon error. The inspect here only decides
        whether to build.
        """
        try:
            attrs: dict[str, Any] | None = self._client.images.get(ref).attrs
        except Exception:  # noqa: BLE001 - see docstring
            return None
        return attrs

    def logs_socket(self, container_id: str) -> Any:
        """The RAW attach socket for the log stream.

        Why not container.logs(stream=True): docker-py 7.2.0 demuxes
        client-side and DISCARDS the frame type, so stderr merges into
        stdout (measured on Docker Desktop 28.3.2) — the digests recorded
        in sandbox_runs would lie. The attach socket keeps the daemon's
        multiplexed framing: _demux sees real frames and stdout/stderr
        stay separated. Same socket trick as copy-in (checkpoint 8).
        """
        return self._client.api.attach_socket(
            str(container_id),
            params={"stdout": 1, "stderr": 1, "stream": 1, "logs": 1},
        )


def command_for(spec: SandboxSpec) -> list[str]:
    """The container command: plain argv, or the supervisor wrapper + argv.

    The wrapper is a FIXED runner script — the user argv arrives only as
    positional parameters ("$@"), never interpolated into the shell string,
    so there is nothing to inject. With a workspace the wrapper supervises:
    it waits for the copy-in ready flag, runs the argv as a child, reports
    the child's exit code on a nonced ENGINE_DONE line, and finally execs a
    long sleep so the container (and its tmpfs workspace) stays up for
    copy-out — the runner tears it down.
    """
    argv = list(spec.argv)
    if not spec.workspace_files:
        return argv
    return ["/bin/sh", "-c", _WRAPPER_SCRIPT, "engine-run", *argv]


def create_kwargs(
    spec: SandboxSpec,
    limits: SandboxLimits,
    name: str,
    labels: dict[str, str],
    done_nonce: str = "",
) -> dict[str, Any]:
    """The exact container-create arguments (the canonical hardening profile).

    API-native values only (docker-py accepts dicts for ulimits/log_config):
    this dict is pinned by tests and documented in ADR 003. We use
    create -> start -> copy_in (exec tar) instead of containers.run: the
    daemon refuses archive PUTs into read-only containers (measured), so
    the workspace copy-in must follow start. With a workspace, the
    supervisor wrapper (command_for) needs its per-run nonce, injected via
    the environment — the only control channel besides argv. There is
    deliberately NO privileged flag, NO bind mount, and no caller-supplied
    raw options.
    """
    env = dict(spec.env)
    if spec.workspace_files:
        env["ENGINE_DONE_NONCE"] = done_nonce
    return {
        "image": spec.image,  # pinned by digest
        "command": command_for(spec),  # argv list; wrapper only for copy-in
        "name": name,
        "network_mode": "none",  # no interfaces, no DNS
        "user": _SANDBOX_USER,  # non-root
        "working_dir": _WORKSPACE_DIR,
        "read_only": True,  # read-only root filesystem
        "tmpfs": {
            "/tmp": "size=16m,noexec,nosuid,nodev",
            # exec stays ALLOWED on the workspace: running untrusted code is
            # the feature; the boundary is the container, not the file system
            _WORKSPACE_DIR: (
                f"size={limits.workspace_mb}m,nosuid,nodev,uid=65534,gid=65534,mode=0755"
            ),
        },
        "environment": env,  # allowlist + runner nonce; no host passthrough
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges"],
        "nano_cpus": int(limits.cpus * 1_000_000_000),  # CPU quota
        "mem_limit": limits.mem_mb * 1024 * 1024,
        "memswap_limit": limits.mem_mb * 1024 * 1024,  # == mem_limit: no swap
        "pids_limit": limits.pids,
        "ulimits": [
            {"Name": "nofile", "Soft": limits.nofile, "Hard": limits.nofile},
            {
                "Name": "fsize",
                "Soft": limits.fsize_mb * 1024 * 1024,
                "Hard": limits.fsize_mb * 1024 * 1024,
            },
        ],
        # caps the DAEMON-side log growth too (the runner caps its own memory
        # separately, but an endless printer would otherwise fill host disk)
        "log_config": {
            "type": "json-file",
            "config": {"max-size": "10m", "max-file": "1"},
        },
        "labels": dict(labels),
        "stdin_open": False,
        "tty": False,  # the demuxer below requires the multiplexed protocol
        "auto_remove": False,  # the runner removes after reading attrs/logs
    }


def _container_name(spec: SandboxSpec) -> str:
    run_key = spec.run_key or uuid.uuid4().hex[:12]
    return f"{_LABEL_PREFIX}-{spec.job_id}-{spec.attempt}-{run_key}"


def _labels(spec: SandboxSpec, started_epoch: float) -> dict[str, str]:
    return {
        _LABEL_PREFIX: "1",
        f"{_LABEL_PREFIX}.created": str(int(started_epoch)),
        f"{_LABEL_PREFIX}.job": spec.job_id,
        f"{_LABEL_PREFIX}.attempt": str(spec.attempt),
    }


def _remove_same_name(client: SandboxClient, name: str) -> None:
    """Retry safety: a previous container with this name must not block us."""
    for stale in client.list_containers({"all": True, "name": name}):
        try:
            stale.remove(force=True)
        except OSError:
            pass  # the janitor will catch it; create() below is the truth


def _kill(container: SandboxContainer) -> None:
    try:
        container.kill(signal="SIGKILL")
    except OSError:
        pass  # already dead, or wedged in D state — removal handles the rest


def _socket_chunks(sock: Any, size: int = 65536) -> Iterator[bytes]:
    """Chunks off the RAW attach socket, until the peer closes or the kill
    tears the stream down. Both transports expose recv(): unix gives a raw
    socket; Windows gives docker-py's named-pipe socket (the copy-in path
    already reaches its underlying handle through ._sock, checkpoint 8)."""
    reader = getattr(sock, "recv", None) or getattr(getattr(sock, "_sock", None), "recv", None)
    if reader is None:
        return
    while True:
        try:
            chunk = reader(size)
        except _DRAIN_ERRORS:
            return  # socket torn down by the kill — expected
        if not chunk:
            return
        yield chunk


class _LogReader:
    """Consumes the multiplexed log stream in a thread, capped per stream.

    Once a stream's cap is hit, further frames of that stream are DISCARDED,
    not buffered: an endless printer cannot grow the runner's memory. The
    thread ends at stream EOF (container death) — the daemon-side log driver
    (json-file, max-size) keeps its own disk bounded meanwhile.

    With a workspace, the supervisor reports the payload's exit code on a
    nonced ENGINE_DONE line; the reader scans for it (frames may split it,
    so a small carry buffer is kept) and raises `done_event` with the code.
    """

    def __init__(
        self,
        container: SandboxContainer,
        cap: int,
        done_nonce: str = "",
        socket: Any | None = None,
    ) -> None:
        self._container = container
        self._cap = cap
        self._stdout = bytearray()
        self._stderr = bytearray()
        self._stdout_truncated = False
        self._stderr_truncated = False
        self._thread: threading.Thread | None = None
        self._done_prefix = _DONE_PREFIX + done_nonce.encode() + b"=" if done_nonce else None
        self._carry = bytearray()
        self.done_code: int | None = None
        self.done_event = threading.Event()
        # When the caller hands over the RAW attach socket (the real Docker
        # adapter, attached BEFORE start), the daemon's framing reaches
        # _demux intact and stdout/stderr stay separated. Without it (unit
        # stubs, older clients) the container.logs() stream is used as-is.
        self._socket = socket
        self._stream: Any | None = None

    def start(self) -> None:
        if self._socket is not None:
            stream = _socket_chunks(self._socket)
        else:
            stream = self._container.logs(stream=True, follow=True, stdout=True, stderr=True)
        self._stream = stream
        self._thread = threading.Thread(target=self._consume, args=(stream,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Unblock the reader, so join() does not wait for the container to die.

        The attach socket stays open as long as the container runs, and the
        supervisor holds the container open on purpose (so the tmpfs workspace
        survives for copy-out). Joining before closing therefore waited the whole
        join timeout — a fixed 10 s per pass, measured. The marker has already
        given us the exit code, so the rest of the stream is not needed.
        """
        for target in (self._socket, self._stream):
            if target is None:
                continue
            try:
                target.close()
            except _DRAIN_ERRORS:
                pass
            except Exception:  # noqa: BLE001 - a stream that refuses to close is not fatal
                pass

    def _consume(self, stream: Any) -> None:
        try:
            for frame_type, payload in _demux(stream):
                if frame_type == _STDOUT_FRAME:
                    buf, truncated_attr = self._stdout, "_stdout_truncated"
                elif frame_type == _STDERR_FRAME:
                    buf, truncated_attr = self._stderr, "_stderr_truncated"
                else:
                    continue
                if len(buf) + len(payload) > self._cap:
                    room = self._cap - len(buf)
                    if room > 0:
                        buf.extend(payload[:room])
                    setattr(self, truncated_attr, True)
                else:
                    buf.extend(payload)
                self._scan_for_done(payload)
        except OSError:
            pass  # socket torn down by the kill — expected

    def _scan_for_done(self, payload: bytes) -> None:
        """Detect the supervisor's ENGINE_DONE marker across frame splits."""
        if self._done_prefix is None or self.done_event.is_set():
            return
        self._carry.extend(payload)
        index = self._carry.find(self._done_prefix)
        if index >= 0:
            digits = bytearray()
            for byte in self._carry[index + len(self._done_prefix) :]:
                if 0x30 <= byte <= 0x39:
                    digits.append(byte)
                else:
                    break
            if digits:
                self.done_code = int(digits)
                self.done_event.set()
        tail = len(self._done_prefix) + 16
        if len(self._carry) > tail:
            del self._carry[:-tail]

    def join(self) -> tuple[bytes, bytes, bool, bool]:
        if self._thread is not None:
            self._thread.join(timeout=_LOG_JOIN_TIMEOUT_SECONDS)
            if self._thread.is_alive():
                self._thread = None  # leaked daemon thread; caps still hold
        return (
            bytes(self._stdout),
            bytes(self._stderr),
            self._stdout_truncated,
            self._stderr_truncated,
        )


def _demux(stream: Any) -> Iterator[tuple[int, bytes]]:
    """Yield (frame_type, payload) from Docker's multiplexed stream protocol.

    Each frame: 8-byte header [type, 0, 0, 0, size:u32be] then the payload.
    Chunks from the stream may split frames arbitrarily. A stream that is
    NOT the multiplexed protocol is yielded whole as stdout: the attach
    socket (the runner's log path, checkpoint 9) always sends real frames,
    but the fallback covers docker-py versions/builds that demux client-
    side and strip the frame types (docker-py 7.2.0 measured), and tty
    mode. A real frame has zeros at bytes 1-4, so a payload chunk that
    merely starts with 0x00-0x02 is not mistaken for a frame header.
    """
    buffer = bytearray()
    for chunk in stream:
        buffer.extend(chunk)
        while len(buffer) >= 8:
            frame_type = buffer[0]
            size = int.from_bytes(buffer[4:8], "big")
            if (
                frame_type not in (0, _STDOUT_FRAME, _STDERR_FRAME)
                or bytes(buffer[1:4]) != b"\x00\x00\x00"
            ):
                # not the multiplexed protocol: yield everything as stdout
                payload = bytes(buffer)
                buffer.clear()
                yield _STDOUT_FRAME, payload
                break
            if len(buffer) < 8 + size:
                break
            payload = bytes(buffer[8 : 8 + size])
            del buffer[: 8 + size]
            yield frame_type, payload
    # stream end: flush a raw stream's final tail — a demuxed payload
    # shorter than one header (e.g. `b"1.16.0\n"`, 7 bytes) is real output
    if buffer and buffer[0] not in (0, _STDOUT_FRAME, _STDERR_FRAME):
        yield _STDOUT_FRAME, bytes(buffer)


def _wait_for_done(
    container: SandboxContainer, done_event: threading.Event, deadline: float
) -> str:
    """Wait for the supervisor marker, container exit, or the wall clock.

    Returns one of "marker" (payload reported its exit code — the container
    is STILL RUNNING, held open by the supervisor's sleep, so copy-out can
    happen now), "exited" (container died without a marker — the exit code
    is in State), or "timeout". container.wait(timeout=...) raises a
    timeout-type exception that differs by SDK version; requests'
    ReadTimeout/ConnectionError and docker API errors all subclass OSError,
    so one except clause covers them all.
    """
    next_liveness = 0.0  # check once immediately: a fast payload may be gone
    while True:
        if done_event.wait(_MARKER_POLL_SECONDS):
            return "marker"
        now = time.monotonic()
        if now >= next_liveness:
            next_liveness = now + _LIVENESS_POLL_SECONDS
            if _container_stopped(container):
                return "exited"  # ended without a marker: State has the code
        if now >= deadline:
            return "timeout"


def _container_stopped(container: SandboxContainer) -> bool:
    """True once the container is no longer running; False while it is.

    A re-read that fails is treated as "still running": the deadline, not a
    guessed state, is what ends the wait. This replaces the old wake-up path,
    where `container.wait(timeout=...)` was both the exit detector and the
    marker detector and cost the whole slice either way.
    """
    try:
        container.reload()
    except OSError:
        return False
    state = container.attrs.get("State") or {}
    status = state.get("Status")
    if status is None:
        # No evidence either way: the deadline decides, never a guess.
        return False
    return status not in ("running", "created", "paused", "restarting")


def _remove_with_retries(container: SandboxContainer) -> tuple[bool, bool]:
    """Remove in a finally; force-kill first. Returns (removed, kill_failed).

    A D-state process survives SIGKILL until its I/O returns, so force-remove
    can hang: we retry a bounded number of times and then LEAVE the container
    for the janitor, recording kill_failed — removal failure is an expected,
    measured outcome, never an exception escaping the runner.
    """
    for attempt in range(_REMOVE_RETRIES):
        try:
            container.remove(force=True)
            return True, False
        except OSError:
            time.sleep(_REMOVE_BACKOFF_SECONDS * (attempt + 1))
    return False, True


def _send_all(sock: Any, data: bytes) -> None:
    """Transport-tolerant copy-in write.

    Three measured shapes:
    - Windows npipe raw socket: has `sendall` — the original path.
    - Linux (SDK 7.2, ubuntu runner): `exec_start(socket=True)` returns the
      http-client's READER stream — a `socket.SocketIO` opened 'rb'. Its
      `write()` exists but RAISES `io.UnsupportedOperation: File or stream
      is not writable.`; the writable side is the underlying socket at
      `sock._sock`. (First CI run died with "no attribute 'sendall'", the
      second with exactly that UnsupportedOperation.)
    - Any other writable RawIOBase: loop `write()` honoring partial writes.
    """
    sendall = getattr(sock, "sendall", None)
    if sendall is not None:
        sendall(data)
        return
    underlying = getattr(sock, "_sock", None)
    if underlying is not None and hasattr(underlying, "sendall"):
        underlying.sendall(data)
        return
    if not hasattr(sock, "write"):
        raise SandboxError("exec socket has no sendall/_sock/write to write through")
    view = memoryview(data)
    while view:
        written = sock.write(view)
        if not written:
            raise SandboxError("exec socket refused a copy-in write (closed?)")
        view = view[written:]


def _drain_chunk(sock: Any, size: int) -> bytes:
    """Transport-tolerant read: npipe's recv(), SocketIO's read()."""
    recv = getattr(sock, "recv", None)
    if recv is not None:
        return bytes(recv(size) or b"")
    return bytes(sock.read(size) or b"")


def copy_in(
    client: SandboxClient,
    container: SandboxContainer,
    files: Mapping[str, bytes],
) -> None:
    """Copy the workspace in through an exec'd tar (the kubectl-cp mechanism).

    Measured on Docker 28.3.2: the daemon REFUSES archive PUTs into
    read-only containers — "container rootfs is marked read-only" — both
    before start and after, even when the target is the mounted tmpfs
    workspace. So the workspace travels as a tar stream piped into
    `tar -xf -` running inside the container (exec processes share the
    container's mount namespace, so the tmpfs is writable to them). The
    ready flag rides in the tar; the fixed wrapper (command_for) holds
    untrusted code until it lands. Raises SandboxError on any copy-in
    failure — copy-in is infrastructure, never a run outcome.
    """
    payload = dict(files)
    payload[_READY_FLAG] = b"1"
    exec_id_result = client.exec_create(
        str(container.id), ["tar", "-xf", "-", "-C", _WORKSPACE_DIR]
    )
    exec_id = str(exec_id_result["Id"])
    sock = client.exec_start(exec_id, socket=True)
    try:
        _send_all(sock, build_workspace_tar(payload))
    except _DRAIN_ERRORS as exc:
        raise SandboxError(f"workspace copy-in failed: {exc}") from exc
    # No half-close here: measured on the Windows named-pipe transport,
    # shutdown() ABORTS the stream (tar exits 128 and never extracts). A tar
    # archive self-terminates on its zero-block trailer, so tar ends on its
    # own once the full archive is consumed.
    drain_error: BaseException | None = None
    try:
        sock.settimeout(_COPY_IN_DRAIN_TIMEOUT_SECONDS)
    except (AttributeError, OSError):
        # Linux SocketIO has no settimeout of its own; its underlying
        # socket does. Unbounded drain is the fallback — the exec
        # exit-code poll below still bounds the overall copy-in.
        underlying = getattr(sock, "_sock", None)
        if underlying is not None:
            try:
                underlying.settimeout(_COPY_IN_DRAIN_TIMEOUT_SECONDS)
            except (AttributeError, OSError):
                pass
    try:
        while _drain_chunk(sock, 65536):
            pass  # drain tar's (tiny, multiplexed) output
    except _DRAIN_ERRORS as exc:
        # Transport variance: on Windows named pipes EOF arrives as a
        # closed-socket RuntimeError, not an empty recv. The exec exit code
        # or the ready-flag check below — not the drain — is the authority:
        # a truncated stream makes tar exit nonzero / the flag never lands.
        drain_error = exc
    deadline = time.monotonic() + _EXEC_EXIT_POLL_SECONDS
    exit_code: int | None = None
    while True:
        try:
            result = client.exec_inspect(exec_id)
        except OSError:
            break  # the daemon reaped the exec (404 NotFound): verify by flag
        raw = result.get("ExitCode") if isinstance(result, dict) else result
        if raw is not None:
            exit_code = int(raw)
            break
        if time.monotonic() > deadline:
            raise SandboxError("workspace copy-in tar never reported an exit")
        time.sleep(0.05)
    if exit_code is not None and exit_code != 0:
        detail = f" (drain error: {drain_error})" if drain_error else ""
        raise SandboxError(f"workspace copy-in tar exited {exit_code}{detail}")
    if exit_code is None:
        # the daemon reaped the exec record before we could inspect it
        # (measured 404 race): fall back to reading the workspace back
        _verify_copy_in(container)


def _verify_copy_in(container: SandboxContainer) -> None:
    """Fallback copy-in check: read the workspace directory back.

    Measured on Docker Desktop (WSL2): single-file get_archive returns 404
    even for a regular file on a RUNNING container, while directory GETs
    work — so the check fetches /workspace and looks for the ready-flag
    member. The flag is the FIRST tar member, so this alone does not prove
    completeness; tar's exit code (when inspectable) is the authority.
    """
    try:
        stream, _stat = container.get_archive(_WORKSPACE_DIR)
        raw = bytearray()
        for chunk in stream:
            if len(raw) + len(chunk) > _COPY_OUT_MAX_BYTES:
                raise SandboxError("workspace archive exceeds the copy-out cap")
            raw.extend(chunk)
    except OSError as exc:
        raise SandboxError("workspace copy-in did not land (workspace unreadable)") from exc
    with tarfile.open(fileobj=io.BytesIO(bytes(raw)), mode="r:") as tar:
        for member in tar:
            if member.name == _READY_FLAG and member.isfile():
                return
    raise SandboxError("workspace copy-in did not land (ready flag missing)")


def _read_oom_counter(client: SandboxClient, container: SandboxContainer) -> int | None:
    """Ground truth for a 137 exit: the cgroup's oom_kill counter (v2).

    Executed while the container is still running (the supervisor's sleep
    holds it open on the marker path). Measured on Docker 28.3.2: the
    container sees its own cgroup at /sys/fs/cgroup (private namespace) and
    memory.events carries an oom_kill line; memory.max mirrors mem_limit.
    Returns None when unreadable (cgroup v1, exec refused, record reaped) —
    the caller then applies its documented fallback.
    """
    try:
        exec_result = client.exec_create(
            str(container.id), ["/bin/sh", "-c", "cat /sys/fs/cgroup/memory.events"]
        )
        exec_id = str(exec_result["Id"])
        raw = bytearray()
        for chunk in client.exec_start(exec_id, stream=True):
            if len(raw) + len(chunk) > 65536:
                return None  # absurd for memory.events: treat as unreadable
            raw.extend(chunk)
        text = ""
        for frame_type, payload in _demux(iter([bytes(raw)])):
            if frame_type == _STDOUT_FRAME:
                text = payload.decode(errors="replace")
        for line in text.splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[0] == "oom_kill":
                return int(fields[1])
    except _DRAIN_ERRORS:
        pass  # exec failed (container mid-teardown, v1, etc.) — unreadable
    return None


def run(
    spec: SandboxSpec,
    client: SandboxClient,
    limits: SandboxLimits | None = None,
    *,
    done_nonce: str = "",
) -> RunResult:
    """Run one sandboxed process. Bounded in wall time, memory, pids, output.

    With a workspace the payload is supervised: copy_in (exec'd tar) lands
    the workspace, the fixed wrapper runs the payload as a child and
    reports its exit code on a nonced ENGINE_DONE line, and the container
    is held open (exec sleep) so the mutated workspace can be copied out
    BEFORE the tmpfs dies with the container. The caller may pass
    done_nonce (tests pin it); otherwise one is generated per run. Only
    infrastructure failures (create/start/copy-in/copy-out) raise
    SandboxError; run outcomes (timeouts, OOM, bad exit codes) are
    returned.
    """
    limits = limits or SandboxLimits()
    if spec.workspace_files and not done_nonce:
        done_nonce = secrets.token_hex(16)
    name = _container_name(spec)
    started = time.monotonic()
    started_epoch = time.time()
    _remove_same_name(client, name)
    try:
        container = client.create(
            **create_kwargs(spec, limits, name, _labels(spec, started_epoch), done_nonce)
        )
    except OSError as exc:
        raise SandboxError(f"sandbox create failed: {exc}") from exc
    completed = False
    attach = getattr(client, "logs_socket", None)
    sock = None
    try:
        # Attach BEFORE start (the docker-run pattern): a fast-exiting
        # payload cannot outpace the log attach, and the daemon's
        # multiplexed framing reaches the reader intact (docker-py 7.x
        # demuxes its logs() client-side and discards the frame types).
        if attach is not None:
            sock = attach(str(container.id))
        container.start()
        reader = _LogReader(container, limits.output_bytes_per_stream, done_nonce, socket=sock)
        reader.start()
        if spec.workspace_files:
            copy_in(client, container, spec.workspace_files)
        done_kind = _wait_for_done(container, reader.done_event, started + limits.wall_seconds)
        timed_out = done_kind == "timeout"
        if timed_out:
            _kill(container)
            try:
                container.wait(timeout=_LOG_JOIN_TIMEOUT_SECONDS)
            except OSError:
                pass  # wedged in D state: removal/janitor takes over

        try:
            container.reload()
        except OSError:
            pass  # attrs may be stale if the daemon is mid-cleanup; tolerate
        state = container.attrs.get("State") or {}
        raw_exit = state.get("ExitCode")
        state_exit = int(raw_exit) if raw_exit is not None else -1
        if done_kind == "marker":
            exit_code = int(reader.done_code) if reader.done_code is not None else -1
        else:
            exit_code = state_exit
        # OOM ground truth, in evidence order: (1) the cgroup's oom_kill
        # counter, readable while the container is held open (marker path,
        # cgroup v2 — measured on Docker 28.3.2); (2) the daemon's
        # State.OOMKilled (set when the MAIN process is OOM-killed); (3)
        # nothing — exit 137 alone is SIGKILL, which a payload can also send
        # to itself, so without evidence it is NOT labeled OOM. On cgroup-v1
        # daemons a CHILD OOM leaves no marker-path counter and no daemon
        # flag (documented residual gap in ADR 003 D7).
        daemon_oom = bool(state.get("OOMKilled"))
        oom_counter: int | None = None
        if done_kind == "marker":
            oom_counter = _read_oom_counter(client, container)
            if oom_counter is not None:
                oom_killed = oom_counter > 0
            else:
                # no cgroup evidence (cgroup v1 or read refused): 137 is
                # SIGKILL, probably the kernel OOM killer — the documented
                # "probably OOM" label
                oom_killed = daemon_oom or exit_code == 137
        else:
            oom_killed = daemon_oom  # container gone: no counter to read

        files: dict[str, bytes] = {}
        symlinks_skipped = 0
        if done_kind == "marker" and spec.workspace_files:
            copied = copy_out(
                client,
                container,
                max_bytes=limits.copy_out_mb * 1024 * 1024,
                max_entries=limits.copy_out_max_entries,
            )
            files = {
                key: value for key, value in copied.files.items() if not key.startswith(".engine_")
            }
            symlinks_skipped = copied.symlinks_skipped

        reader.stop()  # before join: see _LogReader.stop
        stdout, stderr, stdout_truncated, stderr_truncated = reader.join()
        removed, kill_failed = _remove_with_retries(container)
        completed = True
        duration = round(time.monotonic() - started, 3)
        return RunResult(
            job_id=spec.job_id,
            attempt=spec.attempt,
            container_id=container.id if not removed else None,
            exit_code=exit_code,
            timed_out=timed_out,
            oom_killed=oom_killed,
            kill_failed=kill_failed,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            files=files,
            symlinks_skipped=symlinks_skipped,
            duration_seconds=duration,
            oom_counter=oom_counter,
        )
    except OSError as exc:
        raise SandboxError(f"sandbox infrastructure failed: {exc}") from exc
    finally:
        if not completed:
            # copy-in/copy-out/streaming failed after create: never leave a
            # container behind — take it down before propagating the error.
            _kill(container)
            _remove_with_retries(container)
            if sock is not None:
                try:
                    sock.close()
                except _DRAIN_ERRORS:
                    pass


def _rel_name(name: str) -> str:
    """Normalize one archive member path or raise on hostile input."""
    if "\x00" in name:
        raise SandboxUnsafeArchive(f"NUL byte in archive member {name!r}")
    if name.startswith("/"):
        raise SandboxUnsafeArchive(f"absolute archive member {name!r}")
    normalized = posixpath.normpath(name)
    if normalized.startswith("../") or normalized in ("..", "."):
        raise SandboxUnsafeArchive(f"archive member escapes root: {name!r}")
    return normalized


def build_workspace_tar(files: Mapping[str, bytes]) -> bytes:
    """Tar the copy-in workspace. Entries are owned by the sandbox user.

    Copy-in content comes from the runner (rebuilt patch-log workspace), so
    it is trusted — but the same validation runs anyway, as a contract.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        made_dirs: set[str] = set()
        for name in sorted(files):
            rel = _rel_name(name)
            parent = posixpath.dirname(rel)
            parts = parent.split("/") if parent else []
            for i in range(1, len(parts) + 1):
                directory = "/".join(parts[:i])
                if directory in made_dirs:
                    continue
                info = tarfile.TarInfo(directory)
                info.uid = 65534
                info.gid = 65534
                info.mode = 0o755
                info.type = tarfile.DIRTYPE
                info.mtime = 0
                tar.addfile(info)
                made_dirs.add(directory)
            data = files[name]
            info = tarfile.TarInfo(rel)
            info.uid = 65534
            info.gid = 65534
            info.mode = 0o644
            info.size = len(data)
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def copy_out(
    client: SandboxClient,
    container: SandboxContainer,
    max_bytes: int = _COPY_OUT_MAX_BYTES,
    max_entries: int = _COPY_OUT_MAX_ENTRIES,
) -> CopyOut:
    """Copy results out through an exec'd tar and materialize REGULAR FILES
    ONLY.

    Measured on Docker 28.3.2 (Docker Desktop, WSL2): the daemon-side
    archive API cannot see tmpfs contents — GET of /workspace returns only
    the mount-stub directory entry and single-file GETs 404 even for real
    files on a running container — so the workspace is read back the same
    way it was written: `tar -cf -` INSIDE the container, streamed over the
    exec channel. The safe parser refuses absolute paths, `..`, and NUL,
    NEVER materializes symlinks/hardlinks (counted, not followed), skips
    directories and device/fifo/special nodes, rejects duplicate regular
    file entries (a crafted archive must not overwrite silently), and caps
    both the total payload (max_bytes) and the entry count (max_entries) —
    a tar bomb dies here, not in runner memory.
    """
    exec_result = client.exec_create(
        str(container.id), ["tar", "-cf", "-", "-C", _WORKSPACE_DIR, "."]
    )
    exec_id = str(exec_result["Id"])
    raw = bytearray()
    try:
        for chunk in client.exec_start(exec_id, stream=True):
            if len(raw) + len(chunk) > max_bytes:
                raise SandboxError(f"copy-out from {_WORKSPACE_DIR} exceeds {max_bytes} bytes")
            raw.extend(chunk)
    except _DRAIN_ERRORS as exc:
        raise SandboxError(f"workspace copy-out failed: {exc}") from exc
    try:
        result = client.exec_inspect(exec_id)
        exit_code = result.get("ExitCode") if isinstance(result, dict) else result
    except OSError:
        exit_code = None  # record reaped before inspect: the parse below rules
    if exit_code is not None and int(exit_code) != 0:
        raise SandboxError(f"workspace copy-out tar exited {exit_code}")

    stdout = bytearray()
    stderr = bytearray()
    for frame_type, payload in _demux(iter([bytes(raw)])):
        if frame_type == _STDOUT_FRAME:
            stdout.extend(payload)
        elif frame_type == _STDERR_FRAME:
            stderr.extend(payload)
    files, symlinks_skipped = parse_workspace_tar(
        bytes(stdout), max_bytes=max_bytes, max_entries=max_entries
    )
    return CopyOut(files=files, symlinks_skipped=symlinks_skipped, truncated=False)


def parse_workspace_tar(
    data: bytes, *, max_bytes: int, max_entries: int
) -> tuple[dict[str, bytes], int]:
    """The safe extractor, shared by copy-out and the host-side snapshot
    builder (checkpoint 8): materialize REGULAR FILES ONLY from a tar.

    Refuses absolute paths, `..`, and NUL, rejects duplicate regular-file
    entries (a crafted archive must not overwrite silently), never
    materializes symlinks/hardlinks (counted, not followed), skips
    directories and device/fifo nodes, and caps the entry count and each
    file's declared size — huge/sparse headers die in the header. Raises
    SandboxUnsafeArchive for crafted structure and SandboxError for caps.
    """
    try:
        tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:")
    except tarfile.TarError as exc:
        # a truncated or non-tar stream (SandboxUnsafeArchive below is NOT a
        # TarError and still propagates)
        raise SandboxError(f"unreadable workspace archive: {exc}") from exc
    files: dict[str, bytes] = {}
    symlinks_skipped = 0
    seen_files: set[str] = set()
    with tar:
        for entry_count, member in enumerate(tar, start=1):
            if entry_count > max_entries:
                raise SandboxError(
                    f"workspace archive has more than {max_entries} entries (tar bomb refused)"
                )
            if member.issym() or member.islnk():
                symlinks_skipped += 1  # counted, never materialized/followed
                continue
            if not member.isfile():
                continue  # directories, devices, fifos: not results
            rel = _rel_name(member.name)
            if rel in seen_files:
                raise SandboxUnsafeArchive(f"duplicate archive member {rel!r}")
            seen_files.add(rel)
            if member.size > max_bytes:
                raise SandboxError(f"file {rel!r} exceeds the archive cap")
            extracted = tar.extractfile(member)
            if extracted is None:
                continue
            files[rel] = extracted.read()
    return files, symlinks_skipped


def sweep_containers(
    client: SandboxClient,
    *,
    max_age_seconds: float = 1800.0,
    now: float | None = None,
) -> SweepResult:
    """Janitor: remove sandbox containers older than the age limit.

    A worker killed mid-run (SIGKILL'd by an overlord, as in the Phase 1
    chaos run) leaves its sandbox behind; this pass collects them. Removals
    that keep failing are counted, not raised — the next pass retries.
    """
    timestamp = time.time() if now is None else now
    removed = 0
    failed = 0
    for container in client.list_containers({"label": _LABEL_PREFIX}):
        labels = getattr(container, "labels", {}) or {}
        created_label = labels.get(f"{_LABEL_PREFIX}.created", "")
        if not created_label:
            continue  # no age information: never remove what we cannot age
        created = float(created_label)
        if timestamp - created <= max_age_seconds:
            continue
        try:
            container.remove(force=True)
            removed += 1
        except OSError:
            failed += 1
    return SweepResult(removed=removed, failed=failed)


def remove_stale_containers(client: SandboxClient, *, job_id: str, keep_attempt: int) -> int:
    """Remove one job's containers from EARLIER attempts, before a retry.

    A worker killed mid-run leaves its sandbox (and install) containers
    behind; the janitor is the age-based backstop, but a redelivered job
    must not wait for it: the retry sweeps its OWN job's older-attempt
    containers before starting. Containers that cannot be classified
    (missing or garbled attempt label) and removals that keep failing are
    left for the janitor — never removed blind.
    """
    removed = 0
    for container in client.list_containers({"label": f"{LABEL_PREFIX}.job={job_id}"}):
        labels = getattr(container, "labels", {}) or {}
        raw = labels.get(f"{LABEL_PREFIX}.attempt", "")
        try:
            attempt = int(raw)
        except ValueError:
            continue  # unlabeled or garbled: the janitor's rules, not ours
        if attempt >= keep_attempt:
            continue  # this attempt's own containers (or newer)
        try:
            container.remove(force=True)
            removed += 1
        except OSError:
            pass  # wedged in D state: the janitor retries later
    return removed
