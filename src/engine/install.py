"""The install phase (checkpoint 9, task 1): dependency images, built once.

Decision (ADR 003 D2, settled here): the install container has a WRITABLE
rootfs — the previous note said "read_only rootfs, pip writes /opt/deps",
but `container.commit()` captures only the container's writable layer, and
a tmpfs mount is not part of that layer, so a read-only install container
with a tmpfs deps directory would commit NOTHING. The one deliberate
widening is therefore `read_only=False`; every other hardening flag is
kept exactly as the run phase's (non-root user, `cap_drop: ALL`,
`no-new-privileges`, CPU/memory/pids limits, capped logs, wall clock). pip
writes the packages into `/var/tmp/deps` — a standard FHS directory that
is world-writable in the base image, so the non-root user can create it,
and it lives in the WRITABLE LAYER, so `commit()` captures it (it is not
shadowed by any tmpfs the way /tmp is). Rejected: building the image from
a Dockerfile context — that moves the build execution into the daemon
(root, no cap profile, no wall clock we enforce), a strictly wider
boundary for the same job; the container path reuses D5's timeout/kill
machinery unchanged.

The install container runs a FIXED pip command — `--only-binary :all:`
refuses the sdist/build-script path entirely, `--require-hashes` pins every
artifact to the hash recorded at pin time (an unhashed pin fails
immediately, before the network matters). Its only inputs are the base
image (pinned to a digest before use) and the requirements file, copied in
with `put_archive` before start (the daemon's measured refusal of archive
PUTs applies to READ-ONLY containers; this rootfs is writable). No secrets,
no workspace, no environment passes through; the network is the bridge —
this container is the ONLY networked one in the pipeline.

Cache key: `install_key = sha256(requirements bytes + base image digest)`,
tagged `engine-deps:<key>`. Content addressing makes concurrent builds
harmless: two workers race, both compute the same tag, the loser writes
functionally equivalent content under the same name. (Builds are NOT
byte-reproducible — layer timestamps differ — so runs record the image ID
actually used; "functionally equivalent", never "identical content".)
Cross-process coordination on one daemon is a LOCAL FILE LOCK (flock on
POSIX, msvcrt on Windows): inspect -> lock -> double-check -> build. The
lock is released by the kernel if a builder is killed, a waiter bounded by
the builder's wall clock. A registry would only be wanted to share images
across machines — deliberately not built.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import sys
import tarfile
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .sandbox import LABEL_PREFIX, _demux

_IMAGE_REPO = "engine-deps"
_REQUIREMENTS_NAME = "engine-requirements.txt"
_REQUIREMENTS_PATH = f"/var/tmp/{_REQUIREMENTS_NAME}"
_DEPS_PATH = "/var/tmp/deps"
_REQUIREMENTS_MAX_BYTES = 256 * 1024
_WAIT_SLICE_SECONDS = 5.0
_LOG_TAIL_LINES = "40"
_LOG_TAIL_MAX_CHARS = 2000
# A builder holds the lock at most its own wall clock, so a waiter bounded
# above that never starves while a live builder works.
_LOCK_WAIT_SECONDS = 900.0

# The install container's command: a FIXED argv — never a shell string, never
# caller-supplied flags. `--only-binary :all:` refuses sdists (their build
# scripts are arbitrary code); `--require-hashes` refuses unpinned artifacts.
PIP_ARGV = (
    "pip",
    "install",
    "--no-cache-dir",
    "--disable-pip-version-check",
    "--only-binary",
    ":all:",
    "--require-hashes",
    "-r",
    _REQUIREMENTS_PATH,
    "--target",
    _DEPS_PATH,
)


class InstallFailed(RuntimeError):
    """Typed install failure: `.code` classifies it for the caller.

    Codes: pip_failed / too_large / base_image_missing are permanent (bad
    pins, oversized input, base image absent) — the caller treats them as
    non-retryable. timeout / oom / daemon / lock_timeout are transient.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


@dataclass(frozen=True)
class InstallLimits:
    """The install container's resource knobs (numbers, never raw options).

    pip needs more headroom than a test run: bigger tmpfs (wheel staging),
    bigger per-file size cap, longer wall clock.
    """

    mem_mb: int = 512
    cpus: float = 1.0
    pids: int = 64
    wall_seconds: float = 600.0
    tmp_mb: int = 256
    nofile: int = 256
    fsize_mb: int = 256


@dataclass(frozen=True)
class InstallResult:
    """One install outcome. `image` is the ID actually used — recorded per
    run (builds are functionally equivalent, not byte-identical)."""

    install_key: str
    image: str
    base_ref: str
    base_digest: str
    cached: bool
    duration_seconds: float


class InstallContainer(Protocol):
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
        stdout: bool = ...,
        stderr: bool = ...,
        tail: str | None = ...,
    ) -> Any: ...

    def put_archive(self, path: str, data: bytes) -> Any: ...

    def commit(self, repository: str | None = ..., tag: str | None = ...) -> Any: ...

    def reload(self) -> None: ...

    def remove(self, *, force: bool = ...) -> None: ...


class InstallClient(Protocol):
    """Duck-typed slice for the install phase: create + image inspect.

    `create` returns Any so a client typed as the sandbox duck-type (whose
    create returns the sandbox container protocol) still satisfies it
    structurally — the same adapter serves both phases.
    """

    def create(self, **kwargs: Any) -> Any: ...

    def image_attrs(self, ref: str) -> dict[str, Any] | None: ...


def install_key(requirements: bytes, base_digest: str) -> str:
    """The cache key: content of the pins + content identity of the base.

    Domain-separated so a requirements file is never confused with a base
    digest of the same bytes.
    """
    digest = hashlib.sha256()
    digest.update(b"engine-deps-v1\x00")
    digest.update(base_digest.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(requirements)
    return digest.hexdigest()


def _resolve_base(client: InstallClient, base_image: str) -> tuple[str, str]:
    """Pin the base image to a digest before anything derives from it.

    A digest-pinned ref passes through. A tag is inspected once: a registry
    digest (RepoDigests) is preferred — it pins the manifest, so an upstream
    push cannot silently change future builds; a locally-built image with
    no registry digest falls back to its image ID (exact content identity).
    The resolved ref and digest are recorded with every run.
    """
    if "@sha256:" in base_image:
        digest = base_image.split("@", 1)[1]
        if not digest.startswith("sha256:") or len(digest) != len("sha256:") + 64:
            raise InstallFailed("base_image_missing", f"malformed digest ref {base_image!r}")
        return base_image, digest.removeprefix("sha256:")
    attrs = client.image_attrs(base_image)
    if attrs is None:
        raise InstallFailed(
            "base_image_missing",
            f"base image {base_image!r} is not present on this daemon; "
            "pull or build it first (the digest is pinned before use)",
        )
    repo_digests = attrs.get("RepoDigests") or []
    for entry in repo_digests:
        if "@" in str(entry):
            digest = str(entry).split("@", 1)[1]
            return str(entry), digest.removeprefix("sha256:")
    image_id = str(attrs.get("Id") or "")
    if image_id.startswith("sha256:"):
        return image_id, image_id.removeprefix("sha256:")
    raise InstallFailed("base_image_missing", f"cannot pin base image {base_image!r}")


def _requirements_tar(requirements: bytes) -> bytes:
    """The pins travel as a tar member owned by the sandbox user."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo(_REQUIREMENTS_NAME)
        info.uid = 65534
        info.gid = 65534
        info.mode = 0o444
        info.size = len(requirements)
        info.mtime = 0
        tar.addfile(info, io.BytesIO(requirements))
    return buffer.getvalue()


def install_create_kwargs(
    base_ref: str,
    limits: InstallLimits,
    labels: dict[str, str],
) -> dict[str, Any]:
    """The install container's create arguments: the run phase's hardening
    profile with exactly two deltas — `network_mode` (the index) and
    `read_only=False` (the layer `commit()` must capture). Pinned by tests.

    The deps land in /var/tmp/deps: a real layer directory the non-root user
    can create (FHS /var/tmp is world-writable sticky), NOT a tmpfs — tmpfs
    content is invisible to `commit()`, which is why the read-only variant
    of this design was rejected. /tmp is a tmpfs: pip's scratch never bakes
    into the image.
    """
    return {
        "image": base_ref,  # pinned by digest before use
        "command": list(PIP_ARGV),  # fixed argv, never a shell string
        # no name: the daemon assigns one; labels identify the container
        "network_mode": "bridge",  # THE widening: pip needs the index
        "user": "65534:65534",  # non-root, same uid as the run phase
        "working_dir": "/var/tmp",
        "read_only": False,  # the other widening: the layer is the product
        "tmpfs": {
            "/tmp": f"size={limits.tmp_mb}m,noexec,nosuid,nodev,mode=1777",
        },
        "environment": {},  # nothing passes through: no secrets, no proxy
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges"],
        "nano_cpus": int(limits.cpus * 1_000_000_000),
        "mem_limit": limits.mem_mb * 1024 * 1024,
        "memswap_limit": limits.mem_mb * 1024 * 1024,
        "pids_limit": limits.pids,
        "ulimits": [
            {"Name": "nofile", "Soft": limits.nofile, "Hard": limits.nofile},
            {
                "Name": "fsize",
                "Soft": limits.fsize_mb * 1024 * 1024,
                "Hard": limits.fsize_mb * 1024 * 1024,
            },
        ],
        "log_config": {
            "type": "json-file",
            "config": {"max-size": "10m", "max-file": "1"},
        },
        "labels": dict(labels),
        "stdin_open": False,
        "tty": False,
        "auto_remove": False,
    }


def _labels(job_id: str, attempt: int, key: str, epoch: float) -> dict[str, str]:
    return {
        LABEL_PREFIX: "1",
        f"{LABEL_PREFIX}.created": str(int(epoch)),
        f"{LABEL_PREFIX}.job": job_id,
        f"{LABEL_PREFIX}.attempt": str(attempt),
        f"{LABEL_PREFIX}.kind": "install",
        f"{LABEL_PREFIX}.key": key,
    }


def _try_lock(fd: int) -> bool:
    """One non-blocking exclusive lock attempt. True when acquired."""
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass  # close() releases it regardless


@contextlib.contextmanager
def _daemon_lock(path: Path, timeout: float = _LOCK_WAIT_SECONDS) -> Iterator[None]:
    """Exclusive per-daemon file lock (flock / msvcrt), bounded wait.

    The coordination point for concurrent installs of the same key on one
    machine: inspect, lock, double-check, build. The kernel releases the
    lock when a builder is SIGKILL'd; the bounded wait is sized above the
    builder's wall clock so a live builder never starves a waiter.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        deadline = time.monotonic() + timeout
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise InstallFailed(
                    "lock_timeout",
                    f"daemon install lock {path.name} busy for {timeout:.0f}s",
                ) from None
            time.sleep(0.2)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def _log_tail(container: InstallContainer) -> str:
    """The last log lines, demuxed and capped — triage only, never the API."""
    try:
        frames: list[bytes] = []
        for frame_type, payload in _demux(
            container.logs(stream=True, stdout=True, stderr=True, tail=_LOG_TAIL_LINES)
        ):
            if frame_type in (1, 2):
                frames.append(payload)
        text = b"".join(frames).decode("utf-8", errors="replace")
        return text[-_LOG_TAIL_MAX_CHARS:]
    except OSError:
        return "(install container logs unavailable)"


def _remove(container: InstallContainer) -> None:
    try:
        container.remove(force=True)
    except OSError:
        pass  # the janitor (same label namespace) collects stragglers


def _build_dependency_image(
    client: InstallClient,
    *,
    base_ref: str,
    requirements: bytes,
    key: str,
    job_id: str,
    attempt: int,
    limits: InstallLimits,
) -> str:
    """Run the fixed pip command in a hardened container and commit the
    layer. Holds the daemon lock (the caller's)."""
    started = time.monotonic()
    container = client.create(
        **install_create_kwargs(
            base_ref,
            limits,
            _labels(job_id, attempt, key, time.time()),
        )
    )
    try:
        if len(requirements) > _REQUIREMENTS_MAX_BYTES:
            raise InstallFailed(
                "too_large",
                f"requirements exceed {_REQUIREMENTS_MAX_BYTES} bytes",
            )
        # Archive PUTs before start: the measured daemon refusal applies to
        # READ-ONLY containers only. The pins land in /var/tmp — layer
        # space, so commit() keeps them with the image (provenance).
        container.put_archive("/var/tmp", _requirements_tar(requirements))
        container.start()
        deadline = started + limits.wall_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                try:
                    container.kill(signal="SIGKILL")
                except OSError:
                    pass
                raise InstallFailed(
                    "timeout",
                    f"install exceeded {limits.wall_seconds:.0f}s wall clock",
                )
            try:
                container.wait(timeout=min(remaining, _WAIT_SLICE_SECONDS))
                break  # the container exited; State carries the code
            except OSError:
                pass  # slice elapsed: still running
        try:
            container.reload()
        except OSError:
            pass
        state = container.attrs.get("State") or {}
        exit_code = int(state.get("ExitCode", -1))
        if state.get("OOMKilled"):
            raise InstallFailed(
                "oom", f"pip was OOM-killed (exit {exit_code}); {_log_tail(container)}"
            )
        if exit_code != 0:
            # includes the sdist-only case: --only-binary :all: refuses any
            # distribution without a wheel, so the install FAILS LOUDLY —
            # a typed, per-run decision, never a silent fallback to build
            raise InstallFailed("pip_failed", f"pip exited {exit_code}; {_log_tail(container)}")
        container.commit(repository=_IMAGE_REPO, tag=key)
        attrs = client.image_attrs(f"{_IMAGE_REPO}:{key}")
        image_id = str((attrs or {}).get("Id") or "")
        if not image_id:
            raise InstallFailed("daemon", "commit produced no inspectable image under the key")
        return image_id
    finally:
        # commit() captures the layer; the build container itself always
        # comes down (a failed build leaves nothing behind)
        _remove(container)


def ensure_dependency_image(
    client: InstallClient,
    *,
    requirements: bytes,
    base_image: str,
    job_id: str,
    attempt: int,
    limits: InstallLimits | None = None,
    lock_dir: str = "",
    lock_timeout: float | None = None,
) -> InstallResult:
    """Make sure `engine-deps:<key>` exists on this daemon; build if not.

    The coordination is per-daemon, no database transaction held: inspect
    the content-derived tag, take the local file lock, double-check, then
    build under the lock. Two workers racing on the same key serialize on
    the lock; the loser's double-check finds the image and returns it
    cached. Two machines each build their own — sharing needs a registry,
    which is a later decision, not this one.
    """
    started = time.monotonic()
    limits = limits or InstallLimits()
    if not requirements.strip():
        raise InstallFailed("pip_failed", "requirements file is empty")
    base_ref, base_digest = _resolve_base(client, base_image)
    key = install_key(requirements, base_digest)
    ref = f"{_IMAGE_REPO}:{key}"
    directory = Path(lock_dir) if lock_dir else Path(tempfile.gettempdir()) / "engine-locks"
    with _daemon_lock(
        directory / f"{_IMAGE_REPO}-{key}.lock",
        lock_timeout if lock_timeout is not None else _LOCK_WAIT_SECONDS,
    ):
        cached = client.image_attrs(ref)
        if cached is not None and str(cached.get("Id") or ""):
            return InstallResult(
                install_key=key,
                image=str(cached["Id"]),
                base_ref=base_ref,
                base_digest=base_digest,
                cached=True,
                duration_seconds=round(time.monotonic() - started, 3),
            )
        image_id = _build_dependency_image(
            client,
            base_ref=base_ref,
            requirements=requirements,
            key=key,
            job_id=job_id,
            attempt=attempt,
            limits=limits,
        )
        return InstallResult(
            install_key=key,
            image=image_id,
            base_ref=base_ref,
            base_digest=base_digest,
            cached=False,
            duration_seconds=round(time.monotonic() - started, 3),
        )
