"""Install phase (checkpoint 9, task 1), against stub clients: the cache key,
the base-image pinning, the hardened create profile, build/cached outcomes,
typed failures, and the per-daemon flock that serializes concurrent builds."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from engine import install
from engine.install import (
    InstallFailed,
    InstallLimits,
    _daemon_lock,
    _resolve_base,
    ensure_dependency_image,
    install_create_kwargs,
    install_key,
)

_BASE_TAG = "python:3.12-slim"
_BASE_ATTRS = {
    "Id": "sha256:" + "b" * 64,
    "RepoDigests": ["python@sha256:" + "c" * 64],
}
_BASE_DIGEST = "c" * 64
_BASE_DIGEST_REF = f"python@sha256:{_BASE_DIGEST}"
_REQUIREMENTS = b"six==1.16.0 --hash=sha256:" + b"1" * 60 + b"\n"


def _frames(chunks: list[bytes]) -> list[bytes]:
    """Raw docker multiplexed-stream bytes (stdout frames), for _demux."""
    return [
        b"\x01\x00\x00\x00" + len(chunk).to_bytes(4, "big") + chunk for chunk in chunks
    ]


class _StubContainer:
    def __init__(self, client: _StubInstallClient, **kwargs: Any) -> None:
        self.id = "stub-container-id"
        self.attrs: dict[str, Any] = {"State": {"ExitCode": 0, "OOMKilled": False}}
        self.kwargs = kwargs
        self.client = client
        self.events: list[str] = []

    def start(self) -> None:
        self.events.append("start")

    def put_archive(self, path: str, data: bytes) -> None:
        self.events.append(f"put_archive:{path}")
        self.client.put_archives.append((path, data))

    def wait(self, timeout: float | None = None) -> Any:
        if self.client.still_running:
            raise OSError("timeout slice expired: still running")
        if self.client.wait_delay_seconds:
            time.sleep(self.client.wait_delay_seconds)
        self.events.append("wait")
        return {"StatusCode": self.attrs["State"].get("ExitCode", 0)}

    def kill(self, signal: str | None = None) -> None:
        self.events.append(f"kill:{signal}")

    def logs(
        self,
        *,
        stream: bool = True,
        stdout: bool = True,
        stderr: bool = True,
        tail: str | None = None,
    ) -> Any:
        return iter(_frames([self.client.log_tail.encode("utf-8")]))

    def commit(self, repository: str | None = None, tag: str | None = None) -> Any:
        self.events.append(f"commit:{repository}:{tag}")
        self.client.commits.append((str(repository), str(tag)))
        self.client.images[f"{repository}:{tag}"] = {"Id": f"sha256:{'d' * 64}"}
        return f"sha256:{'d' * 64}"

    def reload(self) -> None:
        pass

    def remove(self, *, force: bool = False) -> None:
        self.events.append("remove")
        self.client.removed.append(self)


class _StubInstallClient:
    """create + image_attrs; images records what commit() published."""

    def __init__(self) -> None:
        self.images: dict[str, dict[str, Any]] = {_BASE_TAG: dict(_BASE_ATTRS)}
        self.created: list[_StubContainer] = []
        self.removed: list[_StubContainer] = []
        self.commits: list[tuple[str, str]] = []
        self.put_archives: list[tuple[str, bytes]] = []
        self.log_tail = "Successfully installed six-1.16.0"
        self.still_running = False  # wait() raises OSError while True
        self.wait_delay_seconds = 0.0

    def create(self, **kwargs: Any) -> _StubContainer:
        container = _StubContainer(self, **kwargs)
        self.created.append(container)
        return container

    def image_attrs(self, ref: str) -> dict[str, Any] | None:
        return self.images.get(ref)


def _ensure(
    client: _StubInstallClient,
    tmp_path: Path,
    *,
    requirements: bytes = _REQUIREMENTS,
    **kwargs: Any,
) -> Any:
    return ensure_dependency_image(
        client,
        requirements=requirements,
        base_image=_BASE_TAG,
        job_id="job-1",
        attempt=1,
        lock_dir=str(tmp_path / "locks"),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# the cache key and base pinning
# ---------------------------------------------------------------------------


def test_install_key_is_content_addressed() -> None:
    key = install_key(_REQUIREMENTS, _BASE_DIGEST)
    assert key == install_key(_REQUIREMENTS, _BASE_DIGEST)  # deterministic
    assert install_key(_REQUIREMENTS, "d" * 64) != key  # base digest matters
    assert install_key(_REQUIREMENTS + b"\n", _BASE_DIGEST) != key  # pins matter
    # domain separation: the same bytes in the other slot must not collide
    assert install_key(b"pins", "digest") != install_key(b"digest", "pins")


def test_resolve_base_prefers_a_digest_pinned_ref() -> None:
    client = _StubInstallClient()
    ref, digest = _resolve_base(client, _BASE_DIGEST_REF)
    assert ref == _BASE_DIGEST_REF
    assert digest == _BASE_DIGEST


def test_resolve_base_pins_a_tag_to_the_registry_digest() -> None:
    client = _StubInstallClient()
    ref, digest = _resolve_base(client, _BASE_TAG)
    assert ref == _BASE_DIGEST_REF  # RepoDigests pin the manifest
    assert digest == _BASE_DIGEST


def test_resolve_base_falls_back_to_the_image_id_without_repo_digests() -> None:
    client = _StubInstallClient()
    client.images[_BASE_TAG] = {"Id": "sha256:" + "e" * 64, "RepoDigests": []}
    ref, digest = _resolve_base(client, _BASE_TAG)
    assert ref == "sha256:" + "e" * 64
    assert digest == "e" * 64


def test_resolve_base_missing_image_is_typed() -> None:
    client = _StubInstallClient()
    client.images.pop(_BASE_TAG)
    with pytest.raises(InstallFailed, match="base_image_missing"):
        _resolve_base(client, _BASE_TAG)


# ---------------------------------------------------------------------------
# the hardened create profile
# ---------------------------------------------------------------------------


def test_install_create_kwargs_are_hardened() -> None:
    kwargs = install_create_kwargs(
        _BASE_DIGEST_REF, InstallLimits(), {"engine-sandbox": "1"}
    )
    assert kwargs["image"] == _BASE_DIGEST_REF  # pinned base
    assert kwargs["command"] == list(install.PIP_ARGV)  # the fixed pip argv
    assert "--only-binary" in kwargs["command"]
    assert ":all:" in kwargs["command"]
    assert "--require-hashes" in kwargs["command"]
    assert kwargs["network_mode"] == "bridge"  # the default widening: the index
    assert kwargs["read_only"] is False  # the other widening: the layer
    assert kwargs["user"] == "65534:65534"  # non-root, like the run phase
    assert kwargs["cap_drop"] == ["ALL"]
    assert kwargs["security_opt"] == ["no-new-privileges"]
    assert kwargs["mem_limit"] == kwargs["memswap_limit"]
    assert kwargs["nano_cpus"] == 1_000_000_000
    assert kwargs["pids_limit"] == 64
    assert kwargs["auto_remove"] is False
    assert kwargs["environment"] == {}  # nothing passes through
    # deps land in the LAYER (/var/tmp), pip scratch in the /tmp tmpfs
    assert kwargs["tmpfs"] == {"/tmp": "size=256m,noexec,nosuid,nodev,mode=1777"}
    assert "volumes" not in kwargs and "mounts" not in kwargs  # no secrets/workspace


def test_install_network_is_configurable() -> None:
    """`ENGINE_INSTALL_NETWORK` narrows the one networked container.

    The bridge default is the widest thing in the pipeline: it can reach host
    services published on the gateway. An operator can point installs at a
    user-defined network with filtered egress; the hostile suite then measures
    whether the host port is still reachable (it xfails today).
    """
    default = install_create_kwargs(_BASE_DIGEST_REF, InstallLimits(), {})
    narrowed = install_create_kwargs(
        _BASE_DIGEST_REF, InstallLimits(network="engine-install-egress"), {}
    )
    assert default["network_mode"] == "bridge"
    assert narrowed["network_mode"] == "engine-install-egress"
    # the network is the ONLY thing that changed
    assert {k: v for k, v in narrowed.items() if k != "network_mode"} == {
        k: v for k, v in default.items() if k != "network_mode"
    }


# ---------------------------------------------------------------------------
# build vs cached
# ---------------------------------------------------------------------------


def test_build_puts_pins_before_start_commits_and_records(tmp_path: Path) -> None:
    client = _StubInstallClient()
    result = _ensure(client, tmp_path)
    key = install_key(_REQUIREMENTS, _BASE_DIGEST)
    assert result.cached is False
    assert result.image == f"sha256:{'d' * 64}"
    assert result.install_key == key
    assert result.base_digest == _BASE_DIGEST
    # the created container ran the pinned digest and the fixed argv
    assert len(client.created) == 1
    kwargs = client.created[0].kwargs
    assert kwargs["image"] == _BASE_DIGEST_REF
    assert kwargs["labels"][f"{install.LABEL_PREFIX}.kind"] == "install"
    assert kwargs["labels"][f"{install.LABEL_PREFIX}.job"] == "job-1"
    assert kwargs["labels"][f"{install.LABEL_PREFIX}.attempt"] == "1"
    assert kwargs["labels"][f"{install.LABEL_PREFIX}.key"] == key
    # pins copied in before start, commit under the content tag, container down
    (path, data) = client.put_archives[0]
    assert path == "/var/tmp"
    assert _REQUIREMENTS in data  # the tar carries the pins verbatim
    assert client.created[0].events.index("put_archive:/var/tmp") < client.created[
        0
    ].events.index("start")
    assert client.commits == [("engine-deps", key)]
    assert client.removed == client.created  # the build container comes down


def test_cached_image_short_circuits_the_build(tmp_path: Path) -> None:
    client = _StubInstallClient()
    first = _ensure(client, tmp_path)
    second = _ensure(client, tmp_path)
    assert first.cached is False
    assert second.cached is True
    assert second.image == first.image
    assert second.install_key == first.install_key
    assert len(client.created) == 1  # one build, ever


def test_pip_failure_is_typed_with_the_log_tail(tmp_path: Path) -> None:
    client = _StubInstallClient()
    client.log_tail = "ERROR: No matching distribution found for pkg (sdist refused)"

    def create(**kwargs: Any) -> _StubContainer:
        container = _StubContainer(client, **kwargs)
        container.attrs["State"].update({"ExitCode": 1, "OOMKilled": False})
        client.created.append(container)
        return container

    client.create = create  # type: ignore[method-assign]
    with pytest.raises(InstallFailed) as excinfo:
        _ensure(client, tmp_path)
    assert excinfo.value.code == "pip_failed"
    assert "pip exited 1" in str(excinfo.value)
    assert "No matching distribution found" in str(excinfo.value)  # the tail rides along
    assert client.removed  # the failed build container did not linger


def test_pip_oom_is_typed(tmp_path: Path) -> None:
    client = _StubInstallClient()
    client.log_tail = "Killed\n"

    def create(**kwargs: Any) -> _StubContainer:
        container = _StubContainer(client, **kwargs)
        container.attrs["State"].update({"ExitCode": 137, "OOMKilled": True})
        client.created.append(container)
        return container

    client.create = create  # type: ignore[method-assign]
    with pytest.raises(InstallFailed) as excinfo:
        _ensure(client, tmp_path)
    assert excinfo.value.code == "oom"
    assert "OOM-killed" in str(excinfo.value)


def test_install_wall_clock_kills_and_fails_typed(tmp_path: Path) -> None:
    client = _StubInstallClient()
    client.still_running = True  # wait() keeps raising: the run never ends
    started = time.monotonic()
    with pytest.raises(InstallFailed) as excinfo:
        _ensure(client, tmp_path, limits=InstallLimits(wall_seconds=0.4))
    elapsed = time.monotonic() - started
    assert excinfo.value.code == "timeout"
    assert "wall clock" in str(excinfo.value)
    assert 0.3 < elapsed < 5.0
    assert any("kill:SIGKILL" in event for event in client.created[0].events)
    assert client.removed == client.created


def test_oversized_or_empty_requirements_are_typed(tmp_path: Path) -> None:
    client = _StubInstallClient()
    with pytest.raises(InstallFailed, match="too_large"):
        _ensure(client, tmp_path, requirements=b"x" * (install._REQUIREMENTS_MAX_BYTES + 1))
    with pytest.raises(InstallFailed, match="requirements file is empty"):
        _ensure(client, tmp_path, requirements=b"   \n")


# ---------------------------------------------------------------------------
# the per-daemon flock
# ---------------------------------------------------------------------------


def test_daemon_lock_is_exclusive_across_threads(tmp_path: Path) -> None:
    lock_path = tmp_path / "locks" / "unit.lock"
    held = threading.Event()
    attempts: list[str] = []
    release = threading.Event()

    def holder() -> None:
        with _daemon_lock(lock_path, timeout=5.0):
            held.set()
            release.wait(timeout=5.0)

    thread = threading.Thread(target=holder)
    thread.start()
    held.wait(timeout=5.0)
    with pytest.raises(InstallFailed, match="lock_timeout"):
        with _daemon_lock(lock_path, timeout=0.3):
            attempts.append("should not happen")
    assert attempts == []
    release.set()
    thread.join(timeout=5.0)
    # released: the next acquire succeeds
    with _daemon_lock(lock_path, timeout=5.0):
        pass


def test_concurrent_installs_serialize_one_build_and_share_the_image(
    tmp_path: Path,
) -> None:
    client = _StubInstallClient()
    client.wait_delay_seconds = 0.3  # the build takes a moment
    results: list[Any] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            results.append(_ensure(client, tmp_path, lock_timeout=15.0))
        except BaseException as exc:  # pragma: no cover - failure surfaces below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors
    built = [result for result in results if not result.cached]
    cached = [result for result in results if result.cached]
    assert len(built) == 1  # exactly one build under the lock
    assert len(cached) == 2  # the others double-checked into the cache
    image = built[0].image
    assert all(result.image == image for result in results)
    assert len(client.created) == 1
