"""Hostile sandbox suite (checkpoint 7): real code, real container.

Runs from the HOST python against Docker Desktop (WSL2 backend). Skipped in
CI (no Docker, no docker SDK): every test is gated on ENGINE_SANDBOX_TESTS=1
and a reachable daemon. Each case asserts containment, that the runner stays
responsive, and that no container is left behind.

Every test prints an "OBSERVED ..." line, which is what the checkpoint
report's containment table quotes.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import pytest

try:
    import docker  # type: ignore[import-untyped]
except ImportError:  # CI: the docker SDK is not installed
    docker = None  # type: ignore[assignment]

from engine.sandbox import (  # noqa: E402
    DockerSandboxAdapter,
    SandboxLimits,
    SandboxSpec,
    create_kwargs,
    run,
    sweep_containers,
)

pytestmark = pytest.mark.skipif(
    docker is None or os.environ.get("ENGINE_SANDBOX_TESTS") != "1",
    reason="hostile suite needs the docker SDK + ENGINE_SANDBOX_TESTS=1 (skipped in CI)",
)

BASE_IMAGE = "python:3.12-slim"


def _limits(**overrides: Any) -> SandboxLimits:
    fields: dict[str, Any] = {"wall_seconds": 25.0}
    fields.update(overrides)
    return SandboxLimits(**fields)


def _spec(adapter_digest: str, argv: tuple[str, ...], **overrides: Any) -> SandboxSpec:
    fields: dict[str, Any] = {
        "image": adapter_digest,
        "argv": argv,
        "env": {"HOME": "/tmp", "SANDBOX_MARKER": "v2"},
        "run_key": os.urandom(6).hex(),
        "job_id": "hostile",
    }
    fields.update(overrides)
    return SandboxSpec(**fields)


@pytest.fixture(scope="module")
def docker_env() -> Any:
    assert docker is not None
    try:
        client = docker.from_env()
        client.ping()
    except Exception as exc:  # pragma: no cover - local daemon issues
        pytest.skip(f"docker daemon unavailable: {exc}")
    image = client.images.pull(BASE_IMAGE)
    digests = image.attrs.get("RepoDigests") or []
    if not digests:
        pytest.skip("no RepoDigests available to pin the base image")
    return {"client": client, "digest": digests[0]}


@pytest.fixture()
def sandbox(docker_env: Any, request: pytest.FixtureRequest) -> Any:
    """Yield (adapter, digest); assert no container was left behind."""
    client = docker_env["client"]
    job = f"hostile-{request.node.name[:36]}"
    yield DockerSandboxAdapter(client), docker_env["digest"], job
    leftovers = client.containers.list(
        all=True, filters={"label": f"engine-sandbox.job={job}"}
    )
    assert not leftovers, f"containers left behind: {[c.id for c in leftovers]}"


def test_fork_bomb_contained_by_pids_limit(sandbox: Any) -> None:
    adapter, digest, job = sandbox
    # The classic `:(){ :|:& };:` backgrounds everything and exits 0, so the
    # containment is measured directly: count forks until the kernel refuses.
    code = (
        "import json, os\n"
        "kids = 0\n"
        "try:\n"
        "    while True:\n"
        "        pid = os.fork()\n"
        "        if pid == 0:\n"
        "            os._exit(0)  # children exit at once; zombies pile up\n"
        "        kids += 1\n"
        "except OSError as e:\n"
        "    print(json.dumps({'forked': kids, 'errno': e.errno}), flush=True)\n"
    )
    result = run(
        _spec(digest, ("python", "-c", code), job_id=job),
        adapter,
        _limits(pids=64, wall_seconds=20.0),
    )
    observed = result.stdout.decode(errors="replace").strip()
    print(
        f"OBSERVED fork-bomb: {observed} duration={result.duration_seconds}s "
        f"timed_out={result.timed_out}"
    )
    payload = json.loads(observed)
    assert payload["errno"] == 11  # EAGAIN: the pids cgroup refused the fork
    assert payload["forked"] < 200  # bounded by pids_limit=64, not unbounded
    assert result.duration_seconds < 30.0  # the runner stayed responsive


def test_infinite_loop_killed_by_wall_clock(sandbox: Any) -> None:
    adapter, digest, job = sandbox
    result = run(
        _spec(digest, ("python", "-c", "while True: pass"), job_id=job),
        adapter,
        _limits(wall_seconds=6.0),
    )
    print(
        f"OBSERVED infinite-loop: exit={result.exit_code} timed_out={result.timed_out} "
        f"duration={result.duration_seconds}s kill_failed={result.kill_failed}"
    )
    assert result.timed_out is True
    assert 6.0 <= result.duration_seconds < 6.0 + 20.0
    assert result.exit_code == 137  # SIGKILL from the runner, at the deadline


def test_memory_hog_stopped_by_mem_limit(sandbox: Any) -> None:
    adapter, digest, job = sandbox
    result = run(
        _spec(
            digest,
            ("python", "-c", "xs = []\nwhile True: xs.append(b'x' * (10 ** 7))"),
            job_id=job,
        ),
        adapter,
        _limits(mem_mb=192),
    )
    print(
        f"OBSERVED memory-hog: exit={result.exit_code} oom_killed={result.oom_killed} "
        f"timed_out={result.timed_out} duration={result.duration_seconds}s"
    )
    assert result.timed_out is False
    assert result.exit_code != 0  # OOM-killed (137) or MemoryError (1)


def test_disk_fill_stopped_by_fsize_or_tmpfs(sandbox: Any) -> None:
    adapter, digest, job = sandbox
    code = (
        "data = b'x' * (2 * 1024 * 1024)\n"
        "with open('/workspace/full.bin', 'wb') as f:\n"
        "    try:\n"
        "        while True:\n"
        "            f.write(data)\n"
        "    except OSError as e:\n"
        "        print('FILL-STOPPED', e.errno, flush=True)\n"
    )
    result = run(
        _spec(digest, ("python", "-c", code), job_id=job),
        adapter,
        _limits(),
    )
    observed = result.stdout.decode(errors="replace").strip()
    print(f"OBSERVED disk-fill: stdout={observed!r} exit={result.exit_code}")
    assert "FILL-STOPPED" in observed
    assert observed.split()[1] in ("27", "28")  # EFBIG (fsize) or ENOSPC (tmpfs)


def test_output_flood_capped_not_grown(sandbox: Any) -> None:
    adapter, digest, job = sandbox
    result = run(
        _spec(
            digest,
            ("python", "-c", "while True: print('x' * 1000, flush=True)"),
            job_id=job,
        ),
        adapter,
        _limits(wall_seconds=8.0),
    )
    print(
        f"OBSERVED output-flood: stdout_bytes={len(result.stdout)} "
        f"truncated={result.stdout_truncated} duration={result.duration_seconds}s"
    )
    assert result.stdout_truncated is True
    assert len(result.stdout) == 1024 * 1024  # capped, not accumulated
    assert result.stderr_truncated is False


def test_outbound_tcp_fails(sandbox: Any) -> None:
    adapter, digest, job = sandbox
    code = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 80), timeout=5)\n"
        "    print('TCP-CONNECTED', flush=True)\n"
        "except OSError as e:\n"
        "    print('TCP-REFUSED', type(e).__name__, flush=True)\n"
    )
    result = run(_spec(digest, ("python", "-c", code), job_id=job), adapter)
    observed = result.stdout.decode(errors="replace")
    print(f"OBSERVED outbound-tcp: {observed.strip()!r}")
    assert "TCP-REFUSED" in observed and "TCP-CONNECTED" not in observed


def test_dns_fails(sandbox: Any) -> None:
    adapter, digest, job = sandbox
    code = (
        "import socket\n"
        "try:\n"
        "    socket.getaddrinfo('example.com', 443)\n"
        "    print('DNS-RESOLVED', flush=True)\n"
        "except OSError as e:\n"
        "    print('DNS-FAILED', type(e).__name__, flush=True)\n"
    )
    result = run(_spec(digest, ("python", "-c", code), job_id=job), adapter)
    observed = result.stdout.decode(errors="replace")
    print(f"OBSERVED dns: {observed.strip()!r} duration={result.duration_seconds}s")
    assert "DNS-FAILED" in observed and "DNS-RESOLVED" not in observed


def test_read_only_root_write_refused(sandbox: Any) -> None:
    adapter, digest, job = sandbox
    code = (
        "import os\n"
        "for path, mode in (('/etc/passwd', 'a'), ('/usr/bin/evil', 'wb')):\n"
        "    try:\n"
        "        open(path, mode).close()\n"
        "        print('WRITE-OK', path, flush=True)\n"
        "    except OSError as e:\n"
        "        print('WRITE-REFUSED', path, e.errno, flush=True)\n"
        "try:\n"
        "    os.mkdir('/newdir')\n"
        "    print('MKDIR-OK', flush=True)\n"
        "except OSError as e:\n"
        "    print('MKDIR-REFUSED', e.errno, flush=True)\n"
    )
    result = run(_spec(digest, ("python", "-c", code), job_id=job), adapter)
    observed = result.stdout.decode(errors="replace")
    print(f"OBSERVED read-only-root: {observed.strip()!r}")
    assert "WRITE-OK" not in observed and "MKDIR-OK" not in observed
    assert "WRITE-REFUSED" in observed and "MKDIR-REFUSED" in observed
    # measured errnos: EACCES(13) on root-owned files (DAC denies first),
    # EROFS(30) on creates — every write attempt refused, by two layers
    assert "30" in observed and "13" in observed


def test_host_paths_and_docker_socket_invisible(sandbox: Any) -> None:
    adapter, digest, job = sandbox
    code = (
        "import json, os\n"
        "mounts = open('/proc/mounts').read()\n"
        "print(json.dumps({\n"
        "    'mnt_c_exists': os.path.exists('/mnt/c'),\n"
        "    'mnt_c_in_mounts': '/mnt/c' in mounts,\n"
        "    'docker_sock': os.path.exists('/var/run/docker.sock')"
        " or os.path.exists('/run/docker.sock'),\n"
        "    'root_fs_type': [l.split()[2] for l in mounts.splitlines()"
        " if l.split()[1] == '/'],\n"
        "}), flush=True)\n"
    )
    result = run(_spec(digest, ("python", "-c", code), job_id=job), adapter)
    observed = result.stdout.decode(errors="replace").strip()
    print(f"OBSERVED host-paths: {observed}")
    assert '"mnt_c_exists": false' in observed
    assert '"mnt_c_in_mounts": false' in observed
    assert '"docker_sock": false' in observed
    assert '"root_fs_type": ["overlay"]' in observed


def test_env_allowlist_no_secret_leak(sandbox: Any) -> None:
    adapter, digest, job = sandbox
    os.environ["ENGINE_SANDBOX_CANARY"] = "must-not-leak"  # worker-process secret
    try:
        code = "import json, os\nprint(json.dumps(sorted(os.environ)), flush=True)\n"
        result = run(_spec(digest, ("python", "-c", code), job_id=job), adapter)
    finally:
        os.environ.pop("ENGINE_SANDBOX_CANARY", None)
    observed = result.stdout.decode(errors="replace")
    print(f"OBSERVED env: {observed.strip()}")
    assert "ENGINE_SANDBOX_CANARY" not in observed
    assert "SANDBOX_MARKER" in observed  # the allowlist itself passed through
    for forbidden in ("DATABASE", "SECRET", "TOKEN", "PASSWORD"):
        assert forbidden not in observed


def test_suid_and_icmp_measured(sandbox: Any) -> None:
    adapter, digest, job = sandbox
    # The Debian base image DOES ship suid binaries (passwd, mount, su...);
    # the defense is no-new-privileges + cap_drop, not binary absence, so the
    # test measures the enforcement directly. ICMP raw needs CAP_NET_RAW;
    # ICMP dgram (unprivileged ping) may be allowed by seccomp — recorded.
    code = (
        "import json, socket\n"
        "nnp = [l.split()[1] for l in open('/proc/self/status')\n"
        "       if l.startswith('NoNewPrivs')]\n"
        "results = {'nnp': nnp}\n"
        "for kind, label in ((socket.SOCK_RAW, 'raw'),"
        " (socket.SOCK_DGRAM, 'dgram')):\n"
        "    try:\n"
        "        socket.socket(socket.AF_INET, kind, socket.IPPROTO_ICMP).close()\n"
        "        results[label] = 'socket-ok'\n"
        "    except OSError as e:\n"
        "        results[label] = f'refused-{e.errno}'\n"
        "try:\n"
        "    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM,"
        " socket.IPPROTO_ICMP)\n"
        "    s.sendto(b'x', ('1.1.1.1', 0))\n"
        "    results['send'] = 'sent'\n"
        "except OSError as e:\n"
        "    results['send'] = f'refused-{e.errno}'\n"
        "print(json.dumps(results), flush=True)\n"
    )
    result = run(_spec(digest, ("python", "-c", code), job_id=job), adapter)
    observed = result.stdout.decode(errors="replace")
    print(f"OBSERVED suid/icmp: {observed.strip()!r} exit={result.exit_code}")
    payload = json.loads(observed)
    assert payload["nnp"] == ["1"]  # no-new-privileges: suid bits cannot elevate
    assert str(payload["raw"]).startswith("refused")  # CAP_NET_RAW dropped
    assert str(payload["send"]).startswith("refused")  # nothing leaves: no network


def test_io_uring_setup_measured(sandbox: Any) -> None:
    adapter, digest, job = sandbox
    code = (
        "import ctypes, errno, json\n"
        "libc = ctypes.CDLL(None, use_errno=True)\n"
        "params = ctypes.create_string_buffer(128)\n"
        "res = libc.syscall(ctypes.c_long(425), ctypes.c_uint(4), params)\n"
        "err = ctypes.get_errno()\n"
        "if res >= 0:\n"
        "    import os\n"
        "    os.close(res)\n"
        "print(json.dumps({'ret': int(res), 'errno': err,"
        " 'name': errno.errorcode.get(err, 'NONE')}), flush=True)\n"
    )
    result = run(_spec(digest, ("python", "-c", code), job_id=job), adapter)
    observed = result.stdout.decode(errors="replace").strip()
    print(f"OBSERVED io_uring_setup (syscall 425, x86_64): {observed}")
    payload = json.loads(observed)
    # Measured on Docker 28.3.2 / API 1.51: the DEFAULT seccomp profile blocks
    # io_uring_setup with EPERM (errno 1). If a future daemon allows it, this
    # assert flips to record-only and the report notes "allowed".
    assert payload["ret"] == -1
    assert payload["errno"] == 1  # EPERM


def test_symlink_through_workspace_refused_by_copy_out(sandbox: Any) -> None:
    adapter, digest, job = sandbox
    # Full run() lifecycle: the supervisor copy-in gate, the payload plants
    # symlinks in the tmpfs workspace, the runner copies the workspace out
    # while the container is still held open, and the safe extractor
    # refuses to materialize the links.
    code = (
        "import os\n"
        "os.symlink('/etc/shadow', '/workspace/evil_link')\n"
        "os.symlink('../../etc/passwd', '/workspace/rel_link')\n"
        "print('PLANTED', flush=True)\n"
    )
    result = run(
        _spec(
            digest,
            ("python", "-c", code),
            job_id=job,
            workspace_files={"real.txt": b"payload"},
        ),
        adapter,
    )
    print(
        f"OBSERVED symlink-door: files={sorted(result.files)} "
        f"symlinks_skipped={result.symlinks_skipped} exit={result.exit_code} "
        f"stdout={result.stdout.decode(errors='replace').strip()!r}"
    )
    assert result.exit_code == 0
    assert result.files == {"real.txt": b"payload"}  # regular files only
    assert result.symlinks_skipped == 2  # never materialized, never followed


def test_janitor_removes_stale_keeps_fresh(sandbox: Any, docker_env: Any) -> None:
    adapter, digest, job = sandbox
    client = docker_env["client"]
    stale_name = f"engine-sandbox-{job}-0-stale"
    fresh_name = f"engine-sandbox-{job}-0-fresh"
    now = time.time()
    stale = adapter.create(
        **create_kwargs(
            _spec(digest, ("sleep", "120"), job_id=job),
            _limits(wall_seconds=300.0),
            stale_name,
            {
                "engine-sandbox": "1",
                "engine-sandbox.created": str(int(now - 7200)),  # forged: 2h old
                "engine-sandbox.job": job,
                "engine-sandbox.attempt": "0",
            },
        )
    )
    fresh = adapter.create(
        **create_kwargs(
            _spec(digest, ("sleep", "120"), job_id=job),
            _limits(wall_seconds=300.0),
            fresh_name,
            {
                "engine-sandbox": "1",
                "engine-sandbox.created": str(int(now)),
                "engine-sandbox.job": job,
                "engine-sandbox.attempt": "0",
            },
        )
    )
    try:
        stale.start()
        fresh.start()
        result = sweep_containers(adapter, max_age_seconds=1800.0)
        remaining = {
            c.name
            for c in client.containers.list(
                all=True, filters={"name": f"engine-sandbox-{job}"}
            )
        }
        print(
            f"OBSERVED janitor: removed={result.removed} failed={result.failed} "
            f"remaining={sorted(remaining)}"
        )
        assert stale_name not in remaining  # stale swept
        assert fresh_name in remaining  # fresh kept
    finally:
        for container in (stale, fresh):
            try:
                container.remove(force=True)
            except OSError:
                pass


def test_self_sigkill_is_not_reported_as_oom(sandbox: Any) -> None:
    adapter, digest, job = sandbox
    # exit 137 is SIGKILL, which a payload can send to itself; the cgroup
    # oom_kill counter (read while the container is held open) is the
    # ground truth that keeps this from being labeled oom_killed
    code = "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n"
    result = run(
        _spec(
            digest,
            ("python", "-c", code),
            job_id=job,
            workspace_files={"real.txt": b"payload"},  # marker + counter path
        ),
        adapter,
    )
    print(
        f"OBSERVED self-sigkill: exit={result.exit_code} oom_killed={result.oom_killed} "
        f"timed_out={result.timed_out}"
    )
    assert result.exit_code == 137
    assert result.oom_killed is False  # counter == 0: no kernel OOM kill
    assert result.timed_out is False


def test_child_oom_reports_cgroup_counter(sandbox: Any) -> None:
    adapter, digest, job = sandbox
    # child OOM: the daemon's State.OOMKilled stays False (payload is a
    # CHILD); the cgroup counter is what proves the kill was the kernel
    code = "xs = []\nwhile True: xs.append(b'x' * (10 ** 7))\n"
    result = run(
        _spec(
            digest,
            ("python", "-c", code),
            job_id=job,
            workspace_files={"real.txt": b"payload"},
        ),
        adapter,
        _limits(mem_mb=192),
    )
    print(
        f"OBSERVED child-oom: exit={result.exit_code} oom_killed={result.oom_killed} "
        f"timed_out={result.timed_out} duration={result.duration_seconds}s"
    )
    assert result.exit_code == 137
    assert result.oom_killed is True  # counter > 0, not an inference
    assert result.timed_out is False


def test_tmpfs_fill_many_small_files_stays_clean(sandbox: Any) -> None:
    adapter, digest, job = sandbox
    # fill the 64 MiB workspace tmpfs with 8 KiB files (well under the
    # per-file fsize cap): inside the mem budget the failure is ENOSPC,
    # the payload handles it and exits 0, and the runner stays responsive
    code = (
        "import errno, os\n"
        "os.makedirs('/workspace/fill', exist_ok=True)\n"
        "count = 0\n"
        "try:\n"
        "    while True:\n"
        "        with open(f'/workspace/fill/{count:05d}', 'wb') as f:\n"
        "            f.write(b'x' * 8192)\n"
        "        count += 1\n"
        "except OSError as e:\n"
        "    print('FILL-STOPPED', e.errno, 'files', count, flush=True)\n"
    )
    result = run(_spec(digest, ("python", "-c", code), job_id=job), adapter)
    observed = result.stdout.decode(errors="replace").strip()
    print(f"OBSERVED tmpfs-fill: stdout={observed!r} exit={result.exit_code}")
    assert result.timed_out is False
    assert result.duration_seconds < 25.0  # runner stayed responsive
    assert observed.startswith("FILL-STOPPED")
    assert observed.split()[1] == "28"  # ENOSPC from the tmpfs size cap
    assert result.exit_code == 0  # the payload handled its own failure


def test_tmpfs_pages_charge_against_mem_limit(sandbox: Any) -> None:
    adapter, digest, job = sandbox
    # the coupling the ADR documents: tmpfs pages are shmem charged to the
    # container's memory cgroup. workspace_mb=248 + interpreter overhead
    # crosses mem_mb=256 while filling -> the kernel OOM-kills before
    # ENOSPC can fire. Whichever way the daemon rules it, the failure must
    # be clean and the runner responsive (both outcomes are recorded).
    code = (
        "import errno, os\n"
        "os.makedirs('/workspace/fill', exist_ok=True)\n"
        "count = 0\n"
        "try:\n"
        "    while True:\n"
        "        with open(f'/workspace/fill/{count:05d}', 'wb') as f:\n"
        "            f.write(b'x' * 8192)\n"
        "        count += 1\n"
        "except OSError as e:\n"
        "    print('FILL-STOPPED', e.errno, 'files', count, flush=True)\n"
    )
    result = run(
        _spec(digest, ("python", "-c", code), job_id=job),
        adapter,
        _limits(workspace_mb=248, tmp_mb=4, mem_mb=256, wall_seconds=20.0),
    )
    observed = result.stdout.decode(errors="replace").strip()
    print(
        f"OBSERVED tmpfs-charges-memory: stdout={observed!r} exit={result.exit_code} "
        f"oom_killed={result.oom_killed} timed_out={result.timed_out}"
    )
    assert result.timed_out is False
    assert result.duration_seconds < 20.0  # runner stayed responsive
    filled_to_limit = observed.startswith("FILL-STOPPED") and observed.split()[1] == "28"
    oom = result.exit_code == 137 and result.oom_killed
    assert filled_to_limit or oom  # clean either way; the coupling is real
