"""Real-Docker install-phase checks (checkpoint 9): the writable container
accepts archive PUTs before start, a real hashed install is committed and
the dependency image runs OFFLINE, unpinned pins fail loudly, an sdist-only
pin set fails loudly, the build is cached, and the base tag is pinned to a
registry digest. Same gate as the hostile suite: ENGINE_SANDBOX_TESTS=1."""

from __future__ import annotations

import hashlib
import io
import os
import tarfile
import time
from typing import Any

import pytest

try:
    import docker  # type: ignore[import-untyped]
except ImportError:  # CI: the docker SDK is not installed
    docker = None  # type: ignore[assignment]

from engine import install  # noqa: E402
from engine.install import (  # noqa: E402
    InstallFailed,
    InstallLimits,
    _labels,
    ensure_dependency_image,
    install_create_kwargs,
)
from engine.sandbox import (  # noqa: E402
    LABEL_PREFIX,
    DockerSandboxAdapter,
    SandboxSpec,
)
from engine.sandbox import (
    run as run_sandbox,
)

pytestmark = pytest.mark.skipif(
    docker is None or os.environ.get("ENGINE_SANDBOX_TESTS") != "1",
    reason="install-hostile suite needs the docker SDK + ENGINE_SANDBOX_TESTS=1 (skipped in CI)",
)

BASE_IMAGE = "python:3.12-slim"
# The wheel hash for six 1.16.0 (six-1.16.0-py2.py3-none-any.whl), measured
# by the daemon itself (pip prints Expected/Got on a mismatch).
_SIX_HASH = "8abb2f1d86890a2dfb989f9a77cfcfd3e47c2a354b01111771326f8aa26e0254"
_SIX_PIN = f"six==1.16.0 --hash=sha256:{_SIX_HASH}\n".encode()
# A deliberately WRONG hash: --require-hashes must refuse the download.
_SIX_BAD_HASH = "1e61c37477a1626458e36f7b1d82aa5c9b094fa4802892072e49de9c60c4c926"
_JOB = "install-hostile"


@pytest.fixture(scope="module")
def docker_env() -> Any:
    assert docker is not None
    try:
        client = docker.from_env()
        client.ping()
    except Exception as exc:  # pragma: no cover - local daemon issues
        pytest.skip(f"docker daemon unavailable: {exc}")
    try:
        image = client.images.pull(BASE_IMAGE)
    except Exception:
        try:
            image = client.images.get(BASE_IMAGE)
        except Exception:
            pytest.skip(f"cannot pull or find {BASE_IMAGE} locally")
    digests = image.attrs.get("RepoDigests") or []
    if not digests:
        pytest.skip("no RepoDigests available to pin the base image")
    return {"client": client, "digest": digests[0]}


@pytest.fixture()
def no_leftovers(docker_env: Any, request: pytest.FixtureRequest) -> Any:
    client = docker_env["client"]
    job = f"{_JOB}-{request.node.name[:40]}"
    yield client, job
    leftovers = client.containers.list(
        all=True, filters={"label": f"{LABEL_PREFIX}.job={job}"}
    )
    assert not leftovers, f"containers left behind: {[c.id for c in leftovers]}"


def _limits() -> InstallLimits:
    return InstallLimits(wall_seconds=120.0, mem_mb=384)


def _digest_of(client: Any, ref: str) -> str:
    """The base image's registry digest (RepoDigests[0])."""
    attrs = client.images.get(ref).attrs
    digests = attrs.get("RepoDigests") or []
    assert digests, f"no RepoDigests on {ref}"
    return str(digests[0]).split("@", 1)[1].removeprefix("sha256:")


def _cleanup_images(client: Any, *refs: str) -> None:
    for ref in refs:
        try:
            client.images.remove(ref, force=True)
        except Exception:
            pass


def test_install_container_accepts_put_archive_before_start(no_leftovers: Any) -> None:
    """The measured daemon refusal of archive PUTs applies to READ-ONLY
    containers; the install profile (writable rootfs) takes the pins before
    start, as a non-root user."""
    client, job = no_leftovers
    adapter = DockerSandboxAdapter(client)
    labels = _labels(job, 1, "probe-key", time.time())
    kwargs = install_create_kwargs("python:3.12-slim", _limits(), labels)
    kwargs["command"] = [
        "sh",
        "-c",
        "id -u; cat /var/tmp/engine-requirements.txt; test -w /var/tmp && echo WRITABLE",
    ]
    container = adapter.create(**kwargs)
    try:
        container.put_archive("/var/tmp", _pins_tar(_SIX_PIN))
        container.start()
        result = container.wait(timeout=30)
        assert result["StatusCode"] == 0
        output = container.logs(stdout=True, stderr=False).decode()
        assert "65534" in output, "the install container runs as nobody"
        assert "WRITABLE" in output, "/var/tmp is writable for the sandbox user"
        assert _SIX_PIN.decode().strip() in output, "the pins landed before start"
    finally:
        container.remove(force=True)


def _pins_tar(requirements: bytes) -> bytes:
    """The same tar the install phase builds (a member for the pins)."""
    from engine.install import _requirements_tar

    return _requirements_tar(requirements)


def test_real_install_commits_and_the_deps_image_runs_offline(no_leftovers: Any) -> None:
    """THE install-path test: pins for a real wheel -> hardened pip ->
    commit -> a network-none sandbox FROM the committed image imports the
    package and reads the pins baked into the layer."""
    client, job = no_leftovers
    adapter = DockerSandboxAdapter(client)
    key = install.install_key(_SIX_PIN, _digest_of(client, BASE_IMAGE))
    _cleanup_images(client, f"engine-deps:{key}")  # deterministic: build, don't hit cache
    result = ensure_dependency_image(
        adapter,
        requirements=_SIX_PIN,
        base_image=BASE_IMAGE,
        job_id=job,
        attempt=1,
        limits=_limits(),
    )
    try:
        assert result.cached is False
        assert result.image.startswith("sha256:")
        assert result.base_ref.endswith("@" + "sha256:" + result.base_digest)
        print(f"OBSERVED install build: {result.duration_seconds:.2f}s "
              f"image={result.image[:19]}... base={result.base_ref}")

        # the dependency image runs with the network OFF (the run profile)
        run = run_sandbox(
            SandboxSpec(
                image=result.image,
                argv=("python", "-c", "import six; print(six.__version__)"),
                env={"PYTHONPATH": "/var/tmp/deps"},
                run_key=os.urandom(6).hex(),
                job_id=job,
                attempt=1,
            ),
            adapter,
        )
        assert run.exit_code == 0, run.stderr.decode(errors="replace")
        assert run.stdout.strip() == b"1.16.0"
        print(f"OBSERVED offline run from deps image: exit 0, six {run.stdout.decode().strip()}")

        # the committed layer carries its provenance: the pins themselves
        run = run_sandbox(
            SandboxSpec(
                image=result.image,
                argv=("python", "-c", "print(open('/var/tmp/engine-requirements.txt').read())"),
                env={"PYTHONPATH": "/var/tmp/deps"},
                run_key=os.urandom(6).hex(),
                job_id=job,
                attempt=1,
            ),
            adapter,
        )
        assert run.exit_code == 0
        assert "six==1.16.0" in run.stdout.decode()

        # a second ensure is a cache hit with the SAME image id
        again = ensure_dependency_image(
            adapter,
            requirements=_SIX_PIN,
            base_image=BASE_IMAGE,
            job_id=job,
            attempt=1,
            limits=_limits(),
        )
        assert again.cached is True
        assert again.image == result.image
        assert again.install_key == result.install_key
    finally:
        _cleanup_images(client, f"engine-deps:{result.install_key}")


def test_wrong_hash_is_refused(no_leftovers: Any) -> None:
    """A pin whose hash does not match the artifact: pip refuses the
    download (THESE PACKAGES DO NOT MATCH THE HASHES) — a tampered index
    cannot substitute a wheel under the same version."""
    client, job = no_leftovers
    adapter = DockerSandboxAdapter(client)
    bad_pin = f"six==1.16.0 --hash=sha256:{_SIX_BAD_HASH}\n".encode()
    with pytest.raises(InstallFailed) as excinfo:
        ensure_dependency_image(
            adapter,
            requirements=bad_pin,
            base_image=BASE_IMAGE,
            job_id=job,
            attempt=1,
            limits=_limits(),
        )
    assert excinfo.value.code == "pip_failed"
    assert "DO NOT MATCH THE HASHES" in str(excinfo.value)
    print(f"OBSERVED wrong-hash refusal: {str(excinfo.value)[:120]}")


def test_stdout_and_stderr_stay_separate(no_leftovers: Any) -> None:
    """THE regression test for the docker-py 7.2 finding: its logs() demuxes
    client-side and discards frame types (stderr merged into stdout, and a
    short final payload dropped). The runner reads the RAW attach socket,
    so both streams stay separated — and a 7-byte stdout tail survives."""
    from engine.sandbox import SandboxSpec
    from engine.sandbox import run as run_sandbox

    client, job = no_leftovers
    adapter = DockerSandboxAdapter(client)
    run = run_sandbox(
        SandboxSpec(
            image=BASE_IMAGE,
            argv=(
                "python",
                "-c",
                "import sys; sys.stdout.write('OUT-tail'); "
                "sys.stderr.write('ERR-line'); sys.exit(0)",
            ),
            env={"HOME": "/tmp"},
            run_key=os.urandom(6).hex(),
            job_id=job,
            attempt=1,
        ),
        adapter,
    )
    assert run.exit_code == 0
    assert run.stdout == b"OUT-tail", f"stdout polluted: {run.stdout!r}"
    assert run.stderr == b"ERR-line", f"stderr lost: {run.stderr!r}"
    print("OBSERVED stream separation: stdout='OUT-tail' stderr='ERR-line'")


def test_unpinned_requirements_fail_loudly(no_leftovers: Any) -> None:
    """--require-hashes refuses a version pin without its hash: the install
    FAILS as a typed permanent error; nothing is committed; the build
    container does not linger."""
    client, job = no_leftovers
    adapter = DockerSandboxAdapter(client)
    unpinned = b"six==1.16.0\n"  # no --hash: refused before the network matters
    with pytest.raises(InstallFailed) as excinfo:
        ensure_dependency_image(
            adapter,
            requirements=unpinned,
            base_image=BASE_IMAGE,
            job_id=job,
            attempt=1,
            limits=_limits(),
        )
    assert excinfo.value.code == "pip_failed"
    assert "hash" in str(excinfo.value)
    print(f"OBSERVED unpinned refusal: {str(excinfo.value)[:120]}")
    leftovers = client.containers.list(
        all=True, filters={"label": f"{LABEL_PREFIX}.job={job}"}
    )
    assert not leftovers, "the failed build container was removed"


def test_sdist_only_pins_fail_loudly(no_leftovers: Any) -> None:
    """--only-binary :all: refuses a pin whose only distribution is an
    sdist: the install fails loudly, never silently building from setup.py.
    The sdist is a REAL minimal package served from an in-container
    find-links directory (seeded by archive PUT, no host bind mount)."""
    client, job = no_leftovers
    sdist = _build_sdist("nopywheel", "1.0")
    sdist_hash = hashlib.sha256(sdist).hexdigest()
    requirements = (
        "--find-links file:///var/tmp/findlinks\n"
        f"nopywheel==1.0 --hash=sha256:{sdist_hash}\n"
    ).encode()

    class _SeedFindLinks:
        """Create-proxy: seeds /var/tmp/findlinks right after create, before
        the install phase starts the container."""

        def __init__(self, inner: Any, tar: bytes) -> None:
            self._inner = inner
            self._tar = tar

        def create(self, **kwargs: Any) -> Any:
            container = self._inner.create(**kwargs)
            container.put_archive("/var/tmp", self._tar)
            return container

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    adapter = _SeedFindLinks(DockerSandboxAdapter(client), _findlinks_tar(sdist))
    with pytest.raises(InstallFailed) as excinfo:
        ensure_dependency_image(
            adapter,
            requirements=requirements,
            base_image=BASE_IMAGE,
            job_id=job,
            attempt=1,
            limits=_limits(),
        )
    assert excinfo.value.code == "pip_failed"
    assert "No matching distribution" in str(excinfo.value)
    print(f"OBSERVED sdist-only refusal: {str(excinfo.value)[:140]}")


def _build_sdist(name: str, version: str) -> bytes:
    """A minimal but REAL sdist: setup.py, a package module, PKG-INFO."""
    inner = io.BytesIO()
    with tarfile.open(fileobj=inner, mode="w") as tar:
        files = {
            f"{name}-{version}/setup.py": (
                f"from setuptools import setup\nsetup(name={name!r}, version={version!r})\n"
            ).encode(),
            f"{name}-{version}/{name}/__init__.py": b"# sdist-only test package\n",
            f"{name}-{version}/PKG-INFO": (
                f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
            ).encode(),
        }
        for path, data in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
    import gzip

    gz = io.BytesIO()
    with gzip.GzipFile(fileobj=gz, mode="wb") as zipped:
        zipped.write(inner.getvalue())
    return gz.getvalue()


def _findlinks_tar(sdist: bytes) -> bytes:
    """An archive PUT tar: findlinks/nopywheel-1.0.tar.gz under /var/tmp."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo("findlinks")
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        info.uid = 65534
        info.gid = 65534
        info.mtime = 0
        tar.addfile(info)
        entry = tarfile.TarInfo("findlinks/nopywheel-1.0.tar.gz")
        entry.size = len(sdist)
        entry.mode = 0o444
        entry.uid = 65534
        entry.gid = 65534
        entry.mtime = 0
        tar.addfile(entry, io.BytesIO(sdist))
    return buffer.getvalue()
