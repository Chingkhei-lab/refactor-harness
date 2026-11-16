"""Unit tests for the sandbox runner (checkpoint 7).

These run everywhere (CI has no Docker): the hardening kwargs are pinned,
the demuxer and the safe extractor are exercised directly, and the
timeout/kill/removal-failure/janitor paths run against stub containers.
The real-docker hostile cases live in test_sandbox_hostile.py.
"""

from __future__ import annotations

import io
import tarfile
import threading
import time
from typing import Any

import pytest

from engine.sandbox import (
    _WRAPPER_SCRIPT,
    SandboxError,
    SandboxLimits,
    SandboxSpec,
    SandboxUnsafeArchive,
    _demux,
    build_workspace_tar,
    copy_out,
    create_kwargs,
    run,
    sweep_containers,
)

_JOB = "job-1"
_NAME = "engine-sandbox-job-1-0-runkey"  # pins the naming convention


def _spec(**overrides: Any) -> SandboxSpec:
    fields = {
        "image": "python@sha256:deadbeef",
        "argv": ("/usr/bin/env",),
        "env": {"HOME": "/tmp"},
        "workspace_files": {"hello.txt": b"hi"},
        "run_key": "runkey",
        "job_id": _JOB,
        "attempt": 0,
    }
    fields.update(overrides)
    return SandboxSpec(**fields)


def _limits(**overrides: Any) -> SandboxLimits:
    fields = {"wall_seconds": 0.5}
    fields.update(overrides)
    return SandboxLimits(**fields)


class FakeContainer:
    """Stub implementing the SandboxContainer protocol surface."""

    def __init__(
        self,
        *,
        name: str = _NAME,
        wait_result: Any = None,
        oom: bool = False,
    ):
        self.name = name
        self.id = "c0ffee"
        self._wait_result = wait_result if wait_result is not None else {"StatusCode": 0}
        if isinstance(self._wait_result, dict):
            oom_exit = int(self._wait_result["StatusCode"])
        else:
            oom_exit = int(self._wait_result)
        # real Docker: State mirrors the wait result
        self.attrs: dict[str, Any] = {
            "State": {"OOMKilled": oom, "ExitCode": oom_exit}
        }
        self._wait_delay = 0.0
        self.killed = 0
        self.removed = 0
        self.remove_error: Exception | None = None
        self.started = False
        self.events: list[str] = []

    def start(self) -> None:
        self.started = True
        self.events.append("start")
    def wait(self, timeout: float | None = None) -> Any:
        if self._wait_delay:
            time.sleep(min(self._wait_delay, timeout or self._wait_delay))
            raise TimeoutError("simulated wait timeout")
        return self._wait_result

    def kill(self, signal: str | None = None) -> None:
        self.killed += 1
        self._wait_delay = 0.0

    def logs(
        self,
        *,
        stream: bool = False,
        follow: bool = False,
        stdout: bool = False,
        stderr: bool = False,
    ) -> Any:
        return iter(())

    def reload(self) -> None:
        pass

    def get_archive(self, path: str) -> Any:
        # copy-in verification reads the /workspace DIRECTORY back (measured:
        # single-file GETs 404 on Docker Desktop) and looks for the flag
        return (
            iter(
                [
                    _tar_with_members(
                        [(".engine_ready", tarfile.REGTYPE, b"1")]
                    )
                ]
            ),
            {"name": path},
        )

    def remove(self, *, force: bool = False) -> None:
        if self.remove_error is not None:
            raise self.remove_error
        self.removed += 1


class FakeSocket:
    """Captures what the runner streams into the copy-in exec."""

    def __init__(self) -> None:
        self.sent = b""
        self.shutdown_called = False

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def shutdown(self, how: int) -> None:
        self.shutdown_called = True

    def recv(self, bufsize: int) -> bytes:
        return b""  # EOF: tar produced nothing


def _stdout_frames(data: bytes, chunk: int = 8192) -> list[bytes]:
    """Wrap raw bytes as docker-style stdout frames (frame type 1)."""
    frames = []
    for start in range(0, len(data), chunk):
        piece = data[start : start + chunk]
        frames.append(bytes([1, 0, 0, 0]) + len(piece).to_bytes(4, "big") + piece)
    return frames


OOM_EVENTS_TEXT = "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\noom_group_kill 0\n"


class FakeClient:
    """Stub client: `existing` containers are visible to list() immediately;
    `next` containers are returned by create() in order."""

    def __init__(
        self,
        existing: list[FakeContainer] | None = None,
        next: list[FakeContainer] | None = None,  # noqa: A002
        exec_exit_code: int | None = 0,
        exec_reaped: bool = False,
        exec_stream_payload: bytes | None = None,
        oom_events_text: str | None = OOM_EVENTS_TEXT,
    ) -> None:
        self.existing = list(existing or [])
        self._next = list(next or [])
        self.created: list[FakeContainer] = []
        self.created_kwargs: list[dict[str, Any]] = []
        self.calls: list[str] = []
        self.exec_cmds: list[list[str]] = []
        self.exec_sockets: list[FakeSocket] = []
        self.exec_exit_code = exec_exit_code
        self.exec_reaped = exec_reaped
        # copy-out reads the workspace through `exec_start(stream=True)`;
        # the payload is what the in-container tar produces. Default: a
        # valid empty tar (GNU tar's end-of-archive blocks).
        self.exec_stream_payload = (
            exec_stream_payload
            if exec_stream_payload is not None
            else _tar_with_members([])
        )
        # the cgroup memory.events text the oom-counter exec reads back;
        # None simulates an unreadable counter (cgroup v1 / read refused)
        self.oom_events_text = oom_events_text
        self._exec_cmd_by_id: dict[str, list[str]] = {}
        self._exec_counter = 0

    def create(self, **kwargs: Any) -> FakeContainer:
        self.created_kwargs.append(kwargs)
        self.calls.append("create")
        if self._next:
            container = self._next.pop(0)
        else:
            container = FakeContainer(name=kwargs["name"])
        container.name = kwargs["name"]
        self.created.append(container)
        return container

    def list_containers(self, filters: dict[str, Any]) -> list[FakeContainer]:
        self.calls.append(f"list:{filters}")
        name = filters.get("name")
        all_containers = self.existing + self.created
        if name is not None:
            return [c for c in all_containers if name in c.name]
        return list(all_containers)

    def exec_create(self, container_id: str, cmd: list[str]) -> Any:
        self.exec_cmds.append(cmd)
        if self.created:
            self.created[-1].events.append("exec_create")
        self._exec_counter += 1
        exec_id = f"exec-{self._exec_counter}"
        self._exec_cmd_by_id[exec_id] = cmd
        return {"Id": exec_id}

    def exec_start(
        self, exec_id: str, *, socket: bool = False, stream: bool = False
    ) -> Any:
        if stream:
            cmd = self._exec_cmd_by_id.get(exec_id, [])
            if cmd[:1] == ["/bin/sh"]:  # the oom-counter read (cat)
                data = (self.oom_events_text or "").encode()
            else:  # the copy-out tar
                data = self.exec_stream_payload
            return iter(_stdout_frames(data))
        assert socket is True  # copy-in always hijacks the socket
        sock = FakeSocket()
        self.exec_sockets.append(sock)
        return sock

    def exec_inspect(self, exec_id: str) -> Any:
        if self.exec_reaped:
            # docker.errors.NotFound (an APIError/OSError): the daemon reaps
            # finished exec instances, so inspect can 404 after completion
            raise TimeoutError("No such exec instance")
        return {"ExitCode": self.exec_exit_code}


# --- the hardening profile is pinned (the exact create arguments) ----------


def test_create_kwargs_are_hardened() -> None:
    nonce = "cafe01"
    kwargs = create_kwargs(
        _spec(), _limits(), "engine-sandbox-job-1-runkey", {}, nonce
    )
    assert kwargs["network_mode"] == "none"
    assert kwargs["user"] == "65534:65534"
    assert kwargs["read_only"] is True
    assert kwargs["cap_drop"] == ["ALL"]
    assert kwargs["security_opt"] == ["no-new-privileges"]
    # workspace spec: the supervisor wrapper fronts the argv (via "$@")
    assert kwargs["command"] == [
        "/bin/sh",
        "-c",
        _WRAPPER_SCRIPT,
        "engine-run",
        "/usr/bin/env",
    ]
    # the wrapper's control channel: nonce via env, nothing else added
    assert kwargs["environment"] == {"HOME": "/tmp", "ENGINE_DONE_NONCE": nonce}
    assert kwargs["mem_limit"] == kwargs["memswap_limit"] == 256 * 1024 * 1024
    assert kwargs["pids_limit"] == 64
    assert kwargs["nano_cpus"] == 1_000_000_000
    assert kwargs["working_dir"] == "/workspace"
    assert kwargs["tmpfs"]["/tmp"] == "size=16m,noexec,nosuid,nodev"
    assert (
        kwargs["tmpfs"]["/workspace"]
        == "size=64m,nosuid,nodev,uid=65534,gid=65534,mode=0755"
    )
    ulimits = {u["Name"]: u for u in kwargs["ulimits"]}
    assert ulimits["nofile"]["Hard"] == 256
    assert ulimits["fsize"]["Hard"] == 32 * 1024 * 1024
    assert kwargs["log_config"]["type"] == "json-file"
    assert kwargs["log_config"]["config"] == {"max-size": "10m", "max-file": "1"}
    assert kwargs["auto_remove"] is False
    assert kwargs["tty"] is False
    assert "privileged" not in kwargs
    assert "volumes" not in kwargs and "binds" not in kwargs
    assert "image" in kwargs and kwargs["image"].startswith("python@sha256:")


def test_command_is_plain_argv_without_workspace() -> None:
    # no copy-in: no wrapper, no nonce — the argv list IS the command
    kwargs = create_kwargs(
        _spec(workspace_files={}), _limits(), "engine-sandbox-job-1-runkey", {}
    )
    assert kwargs["command"] == ["/usr/bin/env"]
    assert kwargs["environment"] == {"HOME": "/tmp"}


def test_run_labels_and_removes_same_name_first() -> None:
    stale = FakeContainer(name=_NAME)
    fresh = FakeContainer(name=_NAME)
    client = FakeClient(existing=[stale], next=[fresh])
    run(_spec(), client, _limits())
    # the first list call targeted the stale same-name container
    assert "'name'" in client.calls[0] and "'all'" in client.calls[0]
    assert stale.removed == 1
    labels = client.created_kwargs[0]["labels"]
    assert labels["engine-sandbox"] == "1"
    assert labels["engine-sandbox.job"] == _JOB
    assert labels["engine-sandbox.attempt"] == "0"
    assert float(labels["engine-sandbox.created"]) > 0
    assert client.created_kwargs[0]["name"] == _NAME


def test_run_copies_workspace_in_and_removes_after() -> None:
    container = FakeContainer()
    client = FakeClient(next=[container])
    run(_spec(), client, _limits(), done_nonce="cafe01")
    assert len(client.exec_sockets) == 1
    with tarfile.open(fileobj=io.BytesIO(client.exec_sockets[0].sent)) as tar:
        names = sorted(member.name for member in tar)
    assert names == [".engine_ready", "hello.txt"]  # the ready flag gates exec
    # no half-close: measured on npipe, shutdown() aborts the stream; the
    # archive's zero-block trailer ends tar on its own
    assert not client.exec_sockets[0].shutdown_called
    assert client.exec_cmds == [["tar", "-xf", "-", "-C", "/workspace"]]
    assert container.started and container.removed == 1


class SocketIOLike:
    """Linux transport shape: socket.SocketIO — write()/read() only, and
    write() may deliver fewer bytes than asked (no sendall, no recv).
    Measured in CI: the npipe-only sendall crashed every workspace run on
    Linux runners with `AttributeError: 'SocketIO' object has no attribute
    'sendall'`."""

    def __init__(self) -> None:
        self.sent = bytearray()

    def write(self, data: Any) -> int:
        piece = bytes(data[:2])  # force the partial-write loop
        self.sent.extend(piece)
        return len(piece)

    def read(self, size: int) -> bytes:
        return b""  # EOF: tar produced nothing


class SocketIOLikeClient(FakeClient):
    """exec_start(socket=True) hands back the Linux SocketIO shape."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.socketio = SocketIOLike()

    def exec_start(
        self, exec_id: str, *, socket: bool = False, stream: bool = False
    ) -> Any:
        if socket:
            self.exec_sockets.append(self.socketio)
            return self.socketio
        return super().exec_start(exec_id, socket=socket, stream=stream)


def test_copy_in_writes_through_the_linux_socketio_shape() -> None:
    # the Linux SDK returns socket.SocketIO for a hijacked exec; the
    # copy-in must loop partial writes and drain via read()
    container = FakeContainer()
    client = SocketIOLikeClient(next=[container])
    run(_spec(), client, _limits(), done_nonce="cafe01")
    with tarfile.open(fileobj=io.BytesIO(bytes(client.exec_sockets[0].sent))) as tar:
        assert sorted(m.name for m in tar) == [".engine_ready", "hello.txt"]


def test_run_starts_before_copy_in() -> None:
    # Measured Docker constraint: PUTs into read-only containers are refused
    # ("rootfs is marked read-only"), so the workspace goes in AFTER start,
    # through an exec'd tar — and never before the container runs.
    container = FakeContainer()
    run(_spec(), FakeClient(next=[container]), _limits())
    assert container.events == ["start", "exec_create"]


def test_run_without_workspace_skips_copy_in() -> None:
    container = FakeContainer()
    client = FakeClient(next=[container])
    run(_spec(workspace_files={}), client, _limits())
    assert client.exec_cmds == [] and client.exec_sockets == []
    # and without copy-in there is no wrapper: the argv IS the command
    assert client.created_kwargs[0]["command"] == ["/usr/bin/env"]


def test_run_copy_in_failure_raises_sandbox_error_and_takes_down() -> None:
    container = FakeContainer()
    client = FakeClient(next=[container], exec_exit_code=2)  # tar failed
    with pytest.raises(SandboxError, match="copy-in tar exited 2"):
        run(_spec(), client, _limits())
    assert container.killed == 1 and container.removed == 1  # no leak


def test_copy_in_waits_for_exit_code() -> None:
    # SDK variance: exec_inspect can report ExitCode=None while the tar is
    # still exiting; copy_in must poll instead of trusting the first value.
    from engine.sandbox import copy_in

    container = FakeContainer()
    client = FakeClient(next=[container])
    results = iter([{"ExitCode": None}, {"ExitCode": 0}])
    client.exec_inspect = lambda exec_id: next(results)  # type: ignore[method-assign]
    copy_in(client, container, {"a.txt": b"x"})
    assert len(client.exec_sockets) == 1


def test_copy_in_survives_reaped_exec() -> None:
    # Measured on Docker 28.3.2: the daemon reaps finished exec instances,
    # so exec_inspect can 404 (an OSError) right after completion. copy_in
    # then falls back to reading the ready flag back through get_archive.
    from engine.sandbox import copy_in

    container = FakeContainer()
    client = FakeClient(next=[container], exec_reaped=True)
    copy_in(client, container, {"a.txt": b"x"})
    assert client.exec_sockets[0].sent  # the archive was streamed


def test_copy_in_fails_when_flag_never_lands() -> None:
    from engine.sandbox import copy_in

    container = FakeContainer()
    client = FakeClient(next=[container], exec_reaped=True)

    def workspace_unreadable(path: str) -> Any:
        raise TimeoutError("No such file")  # 404, an OSError

    container.get_archive = workspace_unreadable  # type: ignore[method-assign]
    with pytest.raises(SandboxError, match="did not land"):
        copy_in(client, container, {"a.txt": b"x"})


def test_copy_in_fails_when_flag_missing_from_workspace() -> None:
    from engine.sandbox import copy_in

    container = FakeContainer()
    client = FakeClient(next=[container], exec_reaped=True)

    def workspace_without_flag(path: str) -> Any:
        return (
            iter([_tar_with_members([("real.txt", tarfile.REGTYPE, b"x")])]),
            {"name": path},
        )

    container.get_archive = workspace_without_flag  # type: ignore[method-assign]
    with pytest.raises(SandboxError, match="ready flag missing"):
        copy_in(client, container, {"a.txt": b"x"})


# --- timeout, kill, cleanup -------------------------------------------------


def test_run_times_out_kills_and_returns() -> None:
    container = FakeContainer()
    container._wait_delay = 30.0  # every wait() raises TimeoutError until kill
    started = time.monotonic()
    result = run(_spec(), FakeClient(next=[container]), _limits(wall_seconds=0.4))
    assert result.timed_out is True
    assert result.oom_killed is False
    assert container.killed == 1
    assert container.removed == 1
    assert result.exit_code == -1 or result.exit_code == 0
    assert time.monotonic() - started < 5.0  # the runner stayed responsive


def test_run_removal_failure_records_kill_failed() -> None:
    container = FakeContainer()
    container.remove_error = TimeoutError("simulated wedged remove")
    result = run(_spec(), FakeClient(next=[container]), _limits())
    assert result.kill_failed is True
    assert container.removed == 0
    assert result.container_id == "c0ffee"  # left behind, for the janitor
    # no exception escaped: the run outcome is returned either way


def test_run_records_oom() -> None:
    container = FakeContainer(oom=True, wait_result={"StatusCode": 137})
    result = run(_spec(), FakeClient(next=[container]), _limits())
    assert result.oom_killed is True
    assert result.exit_code == 137
    assert result.timed_out is False


class SupervisedContainer(FakeContainer):
    """Stub for the supervisor pattern: wait() times out like a container
    that is being held open by the wrapper's sleep, so the run only ends
    when the reader thread reports the ENGINE_DONE marker."""

    def __init__(self, frames: list[bytes]) -> None:
        super().__init__()
        self._frames = frames

    def logs(self, **kwargs: Any) -> Any:
        return iter(self._frames)

    def wait(self, timeout: float | None = None) -> Any:
        time.sleep(0.1)  # the container stays up; the log stream keeps flowing
        raise TimeoutError("still running")  # a live container: wait expires


def test_run_reports_marker_exit_code_and_copies_out() -> None:
    # The supervisor path: a nonced ENGINE_DONE marker on stdout carries the
    # payload's exit code; the runner copies the workspace out through the
    # exec'd tar while the container is still held open, and filters
    # engine-owned names from the results.
    from engine.sandbox import _DONE_PREFIX

    nonce = "cafe01"
    marker = _DONE_PREFIX + nonce.encode() + b"=7\n"
    frame = bytes([1, 0, 0, 0]) + len(marker).to_bytes(4, "big") + marker

    client = FakeClient(
        next=[SupervisedContainer([frame])],
        exec_stream_payload=_tar_with_members(
            [
                ("./", tarfile.DIRTYPE, b""),
                ("./.engine_ready", tarfile.REGTYPE, b"1"),
                ("./out/result.txt", tarfile.REGTYPE, b"done"),
                ("./link", tarfile.SYMTYPE, b"/etc/passwd"),
            ]
        ),
    )
    result = run(
        _spec(),
        client,
        _limits(wall_seconds=5.0),
        done_nonce=nonce,
    )
    assert result.exit_code == 7
    assert result.timed_out is False
    assert result.files == {"out/result.txt": b"done"}  # flag filtered
    assert result.symlinks_skipped == 1


def test_run_marker_detection_across_frame_split() -> None:
    from engine.sandbox import _DONE_PREFIX

    nonce = "beef02"
    marker = _DONE_PREFIX + nonce.encode() + b"=3\n"
    half = len(marker) // 2
    frames = [
        bytes([1, 0, 0, 0]) + len(marker[:half]).to_bytes(4, "big") + marker[:half],
        bytes([1, 0, 0, 0]) + len(marker[half:]).to_bytes(4, "big") + marker[half:],
    ]

    result = run(
        _spec(),
        FakeClient(next=[SupervisedContainer(frames)]),
        _limits(wall_seconds=5.0),
        done_nonce=nonce,
    )
    assert result.exit_code == 3


def _marker_run(nonce: str, code: int, **client_kwargs: Any) -> Any:
    """A supervised run whose payload reported `code` on the done marker."""
    from engine.sandbox import _DONE_PREFIX

    marker = _DONE_PREFIX + nonce.encode() + b"=" + str(code).encode() + b"\n"
    frame = bytes([1, 0, 0, 0]) + len(marker).to_bytes(4, "big") + marker
    client = FakeClient(next=[SupervisedContainer([frame])], **client_kwargs)
    return run(_spec(), client, _limits(wall_seconds=5.0), done_nonce=nonce)


def test_marker_137_with_oom_counter_is_ground_truth() -> None:
    # exit 137 is SIGKILL, not proof of OOM; the cgroup's oom_kill counter
    # (read while the container is held open) is the evidence
    result = _marker_run("aa01", 137, oom_events_text="oom_kill 1\n")
    assert result.exit_code == 137
    assert result.oom_killed is True


def test_marker_137_without_oom_evidence_is_self_kill() -> None:
    # the payload killed itself: counter 0 proves NO kernel OOM kill
    result = _marker_run("aa02", 137, oom_events_text=OOM_EVENTS_TEXT)
    assert result.exit_code == 137
    assert result.oom_killed is False


def test_marker_137_unreadable_counter_is_probably_oom() -> None:
    # cgroup v1 / read refused: no evidence — documented "probably OOM" label
    result = _marker_run("aa03", 137, oom_events_text=None)
    assert result.oom_killed is True


def test_run_output_caps_and_truncation() -> None:
    # a stream that prints a 10-frame flood; the reader must stop at the cap
    frame = bytes([1, 0, 0, 0]) + (200_000).to_bytes(4, "big") + b"x" * 200_000
    stream = iter([frame] * 10)

    class FloodedContainer(FakeContainer):
        def logs(self, **kwargs: Any) -> Any:
            return stream

    result = run(
        _spec(),
        FakeClient(next=[FloodedContainer()]),
        _limits(output_bytes_per_stream=1024 * 1024),
    )
    assert result.stdout_truncated is True
    assert len(result.stdout) == 1024 * 1024
    assert result.stderr_truncated is False


# --- demuxer and safe extractor ---------------------------------------------


def test_demux_splits_partial_frames() -> None:
    frame_a = bytes([1, 0, 0, 0]) + (3).to_bytes(4, "big") + b"abc"
    frame_b = bytes([2, 0, 0, 0]) + (2).to_bytes(4, "big") + b"de"
    stream = iter([frame_a[:5], frame_a[5:] + frame_b[:9], frame_b[9:]])
    frames = list(_demux(stream))
    assert frames == [(1, b"abc"), (2, b"de")]


def test_demux_falls_back_for_non_multiplexed_stream() -> None:
    stream = iter([b"raw output without headers"])
    frames = list(_demux(stream))
    assert frames == [(1, b"raw output without headers")]


def _tar_with_members(members: list[tuple[str, int, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, kind, payload in members:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                info.type = kind
                info.linkname = payload.decode()
                info.size = 0
                tar.addfile(info)
            elif kind == tarfile.DIRTYPE:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            elif kind in (tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE):
                info.type = kind
                info.size = 0
                tar.addfile(info)
            else:
                tar.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def test_copy_out_materializes_regular_files_only() -> None:
    # GNU tar (exec'd inside the container) names members "./..." — pinned
    # here; the safe parser normalizes them back to workspace-relative names.
    archive = _tar_with_members(
        [
            ("./", tarfile.DIRTYPE, b""),
            ("./results/out.txt", tarfile.REGTYPE, b"ok"),
            ("./evil", tarfile.SYMTYPE, b"/etc/passwd"),
            ("./.engine_ready", tarfile.REGTYPE, b"1"),  # run() filters this
        ]
    )
    client = FakeClient(exec_stream_payload=archive)
    result = copy_out(client, FakeContainer())
    assert result.files == {"results/out.txt": b"ok", ".engine_ready": b"1"}
    assert result.symlinks_skipped == 1


def test_copy_out_rejects_escaping_members() -> None:
    client = FakeClient(
        exec_stream_payload=_tar_with_members([("./../host.txt", tarfile.REGTYPE, b"pwn")])
    )
    with pytest.raises(SandboxUnsafeArchive):
        copy_out(client, FakeContainer())

    client = FakeClient(
        exec_stream_payload=_tar_with_members([("/etc/shadow", tarfile.REGTYPE, b"pwn")])
    )
    with pytest.raises(SandboxUnsafeArchive):
        copy_out(client, FakeContainer())


def test_copy_out_skips_hardlinks_devices_and_fifos() -> None:
    archive = _tar_with_members(
        [
            ("./hard", tarfile.LNKTYPE, b"./real.txt"),
            ("./dev", tarfile.CHRTYPE, b""),
            ("./blk", tarfile.BLKTYPE, b""),
            ("./fifo", tarfile.FIFOTYPE, b""),
            ("./real.txt", tarfile.REGTYPE, b"ok"),
        ]
    )
    client = FakeClient(exec_stream_payload=archive)
    result = copy_out(client, FakeContainer())
    assert result.files == {"real.txt": b"ok"}
    assert result.symlinks_skipped == 1  # hardlinks counted with symlinks


def test_copy_out_rejects_duplicate_file_entries() -> None:
    # a crafted archive must not overwrite a result silently
    archive = _tar_with_members(
        [
            ("./dup.txt", tarfile.REGTYPE, b"first"),
            ("./dup.txt", tarfile.REGTYPE, b"second"),
        ]
    )
    client = FakeClient(exec_stream_payload=archive)
    with pytest.raises(SandboxUnsafeArchive, match="duplicate"):
        copy_out(client, FakeContainer())


def test_copy_out_preserves_case_distinct_names() -> None:
    # "Out.txt" and "out.txt" are distinct Linux names. The copy-out lands
    # in runner memory / the patch log — the runner never extracts onto a
    # host filesystem — so both survive; the tool layer re-checks at apply
    # time (checkpoint 8).
    archive = _tar_with_members(
        [("./Out.txt", tarfile.REGTYPE, b"A"), ("./out.txt", tarfile.REGTYPE, b"a")]
    )
    client = FakeClient(exec_stream_payload=archive)
    result = copy_out(client, FakeContainer())
    assert result.files == {"Out.txt": b"A", "out.txt": b"a"}


def test_copy_out_caps_entry_count_tar_bomb() -> None:
    archive = _tar_with_members(
        [(f"./f{i:03d}.txt", tarfile.REGTYPE, b"x") for i in range(30)]
    )
    client = FakeClient(exec_stream_payload=archive)
    with pytest.raises(SandboxError, match="more than 10 entries"):
        copy_out(client, FakeContainer(), max_entries=10)


def test_copy_out_caps_huge_declared_file() -> None:
    # a lying/sparse header declaring 2 GiB with no data behind it: refused
    # before any read, from the header alone
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo("./bomb.bin")
        info.size = 2 * 1024**3
        tar.addfile(info)
    client = FakeClient(exec_stream_payload=buffer.getvalue())
    with pytest.raises(SandboxError, match="exceeds the copy-out cap"):
        copy_out(client, FakeContainer(), max_bytes=1024 * 1024)


def test_copy_out_caps_total_payload() -> None:
    client = FakeClient(
        exec_stream_payload=_tar_with_members([("./big.bin", tarfile.REGTYPE, b"x" * 100)])
    )
    with pytest.raises(SandboxError, match="exceeds 10 bytes"):
        copy_out(client, FakeContainer(), max_bytes=10)


def test_copy_out_survives_reaped_exec() -> None:
    # the daemon can reap the exec record before exec_inspect; the archive
    # parse (not the inspect) is then the authority
    archive = _tar_with_members([("./out.txt", tarfile.REGTYPE, b"ok")])
    client = FakeClient(exec_stream_payload=archive, exec_reaped=True)
    result = copy_out(client, FakeContainer())
    assert result.files == {"out.txt": b"ok"}


def test_rel_name_rejects_nul_directly() -> None:
    # The tar format itself truncates member names at NUL, so a NUL cannot
    # survive a real archive round-trip; the validator is belt-and-braces and
    # is tested directly.
    from engine.sandbox import _rel_name

    with pytest.raises(SandboxUnsafeArchive):
        _rel_name("a\x00b")


def test_build_workspace_tar_rejects_hostile_names() -> None:
    with pytest.raises(SandboxUnsafeArchive):
        build_workspace_tar({"/abs": b"x"})
    with pytest.raises(SandboxUnsafeArchive):
        build_workspace_tar({"../up": b"x"})
    with pytest.raises(SandboxUnsafeArchive):
        build_workspace_tar({"a\x00b": b"x"})


def test_build_workspace_tar_creates_parents_owned_by_sandbox_user() -> None:
    data = build_workspace_tar({"pkg/sub/mod.py": b"print(1)"})
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        infos = {member.name: member for member in tar}
    assert set(infos) == {"pkg", "pkg/sub", "pkg/sub/mod.py"}
    for info in infos.values():
        assert info.uid == 65534 and info.gid == 65534
    assert infos["pkg/sub/mod.py"].size == len(b"print(1)")


# --- janitor -----------------------------------------------------------------


class SweepFake:
    def __init__(self, containers: list[Any]) -> None:
        self.containers = containers

    def create(self, **kwargs: Any) -> Any:
        raise AssertionError("janitor never creates")

    def list_containers(self, filters: dict[str, Any]) -> list[Any]:
        return list(self.containers)


class Sweepable:
    def __init__(self, labels: dict[str, str], *, broken: bool = False) -> None:
        self.labels = labels
        self.removed = 0
        self.broken = broken

    def remove(self, *, force: bool = False) -> None:
        if self.broken:
            raise TimeoutError("wedged")
        self.removed += 1


def test_janitor_sweeps_old_and_keeps_fresh() -> None:
    now = 1_000_000.0
    old = Sweepable({"engine-sandbox.created": str(now - 7200)})
    fresh = Sweepable({"engine-sandbox.created": str(now - 60)})
    broken = Sweepable({"engine-sandbox.created": str(now - 7200)}, broken=True)
    result = sweep_containers(
        SweepFake([old, fresh, broken]), max_age_seconds=3600, now=now
    )
    assert result.removed == 1
    assert result.failed == 1
    assert old.removed == 1 and fresh.removed == 0 and broken.removed == 0


def test_janitor_skips_containers_without_created_label() -> None:
    now = 1_000_000.0
    unlabeled = Sweepable({})
    result = sweep_containers(SweepFake([unlabeled]), max_age_seconds=60, now=now)
    assert result.removed == 0
    assert unlabeled.removed == 0


# --- log reader concurrency ---------------------------------------------------


def test_log_reader_joins_after_container_death() -> None:
    from engine.sandbox import _LogReader

    frame = bytes([1, 0, 0, 0]) + (5).to_bytes(4, "big") + b"hello"

    class DyingContainer(FakeContainer):
        def logs(self, **kwargs: Any) -> Any:
            def generate() -> Any:
                yield frame
                yield frame  # then EOF (container died)

            return generate()

    reader = _LogReader(DyingContainer(), 1024)
    reader.start()
    stdout, _stderr, out_trunc, err_trunc = reader.join()
    assert stdout == frame[8:] * 2
    assert out_trunc is False and err_trunc is False


def test_demux_thread_never_grows_past_cap() -> None:
    from engine.sandbox import _LogReader

    payload = b"x" * 4096
    header = bytes([1, 0, 0, 0]) + len(payload).to_bytes(4, "big")
    done = threading.Event()

    def endless() -> Any:
        while not done.is_set():
            yield header + payload

    class Flooder(FakeContainer):
        def logs(self, **kwargs: Any) -> Any:
            return endless()

    reader = _LogReader(Flooder(), 8192)
    reader.start()
    time.sleep(0.2)
    done.set()
    stdout, _stderr, out_trunc, _err_trunc = reader.join()
    assert len(stdout) == 8192
    assert out_trunc is True
