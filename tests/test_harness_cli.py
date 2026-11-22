"""CLI cases: what a user sees, observed as a value.

The program under test is a real subprocess in a fresh directory, so these tests
check the three things that make a CLI observation trustworthy: the environment is
controlled (no user config, fixed width, no TTY), the inputs land where the case
says, and the output — exit status, streams, written files — comes back as one
comparable value.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from harness import determinism
from harness.cases import Case, case_set_hash, load_cases
from harness.runner import run_pass

REPO = Path(__file__).resolve().parents[1]

PROGRAM = '''\
import os, sys

text = sys.stdin.read()
print("argv", sys.argv[1:])
print("columns", os.environ.get("COLUMNS"))
# sorted(): os.listdir order is filesystem-dependent, and this test failed
# on CI because ext4 returned tmp before .config.
print("home-empty", sorted(os.listdir(os.environ["HOME"])) == [".config", "tmp"])
print("xdg-empty", sorted(os.listdir(os.environ["XDG_CONFIG_HOME"])) == [])
print("tty", sys.stdout.isatty())
print("stdin", text.strip())
with open("written.txt", "w", encoding="utf-8") as handle:
    handle.write(text.upper())
if "--fail" in sys.argv:
    sys.exit(3)
if "--die" in sys.argv:
    os.kill(os.getpid(), 9)
'''


def _target(tmp_path: Path) -> Path:
    checkout = tmp_path / "target"
    checkout.mkdir()
    (checkout / "program.py").write_text(PROGRAM, encoding="utf-8")
    return checkout


def _run(tmp_path: Path, cases: list[Case], checkout: Path) -> dict[str, object]:
    passed = run_pass(
        python=sys.executable,
        checkout=checkout,
        shim_dir=determinism.write_shim(tmp_path / "shim"),
        src_dir=REPO / "src",
        target_paths=[checkout],
        cases=cases,
        index=1,
        module="",
        workdir=tmp_path / "work",
        expected_case_set_hash=case_set_hash(cases),
    )
    return {obs.case_id: obs for obs in passed.observations}


def test_a_cli_case_observes_argv_stdin_exit_and_written_files(tmp_path: Path) -> None:
    checkout = _target(tmp_path)
    cases = [
        Case(
            id="0001-cli",
            source="cli",
            entry="program.py",
            args=(),
            kind="cli",
            argv=("--max-l=80", "input.py"),
            stdin="hello\n",
            files={"input.py": "x = 1\n"},
        )
    ]
    observation = _run(tmp_path, cases, checkout)["0001-cli"]
    assert observation.outcome == "return", observation.exception
    value = observation.value_body()
    assert value["exit"] == 0
    lines = value["stdout"].splitlines()
    assert lines[0] == "argv ['--max-l=80', 'input.py']"
    assert lines[1] == "columns 80"
    assert lines[2] == "home-empty True"
    assert lines[3] == "xdg-empty True"
    assert lines[4] == "tty False"
    assert lines[5] == "stdin hello"
    written = value["files"]["written.txt"]
    # 7 not 6: text mode on Windows writes CRLF, so assert the manifest is
    # self-consistent instead of pinning a platform-dependent size.
    assert written["size"] in (6, 7), written
    assert len(written["sha256"]) == 64
    # The input file is not in the manifest: it was not written by the program.
    assert "input.py" not in value["files"]


def test_the_exit_status_is_part_of_the_value(tmp_path: Path) -> None:
    checkout = _target(tmp_path)
    cases = [
        Case(id="0001-ok", source="cli", entry="program.py", args=(), kind="cli"),
        Case(
            id="0002-fail",
            source="cli",
            entry="program.py",
            args=(),
            kind="cli",
            argv=("--fail",),
        ),
    ]
    by_case = _run(tmp_path, cases, checkout)
    assert by_case["0001-ok"].value_body()["exit"] == 0
    assert by_case["0002-fail"].value_body()["exit"] == 3


@pytest.mark.skipif(sys.platform == "win32", reason="no SIGKILL-by-number on Windows")
def test_a_cli_signal_death_is_an_observation(tmp_path: Path) -> None:
    """A program that kills itself is a behaviour, and CLI cases record it."""
    checkout = _target(tmp_path)
    cases = [
        Case(
            id="0001-die",
            source="cli",
            entry="program.py",
            args=(),
            kind="cli",
            argv=("--die",),
        )
    ]
    observation = _run(tmp_path, cases, checkout)["0001-die"]
    assert observation.outcome == "return", observation.exception
    value = observation.value_body()
    assert value["exit"] is None
    assert value["signal"] == 9


def test_a_case_file_may_not_smuggle_a_path(tmp_path: Path) -> None:
    payload = {
        "cases": [
            {
                "id": "0001-evil",
                "source": "cli",
                "entry": "program.py",
                "kind": "cli",
                "files": {"../escape.py": "x"},
            }
        ]
    }
    path = tmp_path / "cases.json"
    path.write_text(__import__("json").dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="escapes the case directory"):
        load_cases(path)


def test_an_unknown_kind_is_refused(tmp_path: Path) -> None:
    payload = {
        "cases": [
            {"id": "0001", "source": "x", "entry": "e", "kind": "shell"}
        ]
    }
    path = tmp_path / "cases.json"
    path.write_text(__import__("json").dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="kind must be"):
        load_cases(path)


def test_the_shipped_stage2_case_set_matches_its_recorded_hash() -> None:
    """The committed case file is the one the reports cite.

    A case file edited without regenerating its hash would compare observations
    from one set against the hash of another; the runner refuses that at run time,
    and this refuses it at test time.
    """
    path = REPO / "harness" / "cases" / "pycodestyle-cli.json"
    payload = __import__("json").loads(path.read_text(encoding="utf-8"))
    cases = load_cases(path)
    assert len(cases) == 86
    assert payload["case_set_hash"] == case_set_hash(cases)
    # A literal. The hash must be the same on Windows and on the Linux runner —
    # it was not, because `sorted(Path)` is case-normalised on one platform and
    # case-sensitive on the other, and this test is what makes that drift fail
    # here instead of in a report.
    assert payload["case_set_hash"] == (
        "9ddc0ae083a571b8a81e672711398532bf00bec3ada584e6b989787864055043"
    )
    assert payload["counts_by_source"] == {"api": 2, "fixtures": 42, "probes": 42}
    assert {case.kind for case in cases} == {"cli", "api"}
    for case in cases:
        # Inputs only, and nothing that could write outside the case directory.
        assert not any(name.startswith("/") or ".." in Path(name).parts
                       for name in case.files)
    unstable = {c.id for c in cases if "benchmark" in c.id or "jobs" in c.id}
    assert unstable == {"probe-benchmark", "probe-jobs"}
    assert len({c.id for c in cases}) == len(cases), "case ids must be unique"


def test_the_shim_can_start_coverage_but_is_inert_without_the_variable() -> None:
    """Stage-2 coverage needs a hook in the grandchild, and it must be harmless.

    A CLI case runs the target as a subprocess of the harness child, so ordinary
    coverage of the child never sees the target's lines. `COVERAGE_PROCESS_START`
    is the only hook that reaches an arbitrary grandchild; it is installed in the
    shim's `sitecustomize` and guarded by the variable, so a normal pass pays one
    lookup. This test pins both halves: the hook is there, and without the
    variable the shim imports nothing extra.
    """
    from harness.determinism import shim_source

    source = shim_source()
    assert "COVERAGE_PROCESS_START" in source
    assert "process_startup" in source
    compile(source, "sitecustomize", "exec")

    shim = Path(__file__).resolve().parents[1] / ".harness" / "shim-hook-test"
    shim.mkdir(parents=True, exist_ok=True)
    (shim / "sitecustomize.py").write_text(source, encoding="utf-8")
    import subprocess

    env = {"PYTHONPATH": str(shim), "PATH": str(Path(sys.executable).parent)}
    clean = subprocess.run(
        [sys.executable, "-c", "import sitecustomize, sys; print('coverage' in sys.modules)"],
        capture_output=True, text=True, env=env,
    )
    assert clean.returncode == 0, clean.stderr[-400:]
    assert clean.stdout.strip() == "False", "coverage must not load without the variable"


def test_a_traceback_is_observed_as_type_message_and_calls_not_as_source() -> None:
    """A traceback must not leak the target's path, line numbers or source text.

    Measured: two CLI cases print a traceback and a warning that names the
    target's own file and line. With the raw streams compared, *every* mutant
    differed on those two cases — 38 of 100 "kills" — and the two differences the
    migration produced that differed only by line number had to be approved. The
    projection keeps the exception, its message and the frame names.
    """
    from harness.projections import canonicalise_stream

    raw = (
        'Traceback (most recent call last):\n'
        '  File "/tmp/a/old/pycodestyle.py", line 2717, in <module>\n'
        "    _main()\n"
        "    ^^^^^^^\n"
        '  File "/tmp/a/old/pycodestyle.py", line 2607, in read_config\n'
        "    value = config.getint(section, opt)\n"
        "            ^^^^^^^^^^^^^^^^^^^^^^^^^^^\n"
        "ValueError: invalid literal for int() with base 10: 'x'\n"
    )
    moved = raw.replace("/tmp/a/old", "/tmp/b/mutant").replace("2717", "2095").replace(
        "2607", "2002"
    )
    assert canonicalise_stream(raw) == canonicalise_stream(moved)
    projected = canonicalise_stream(raw)
    assert "ValueError: invalid literal for int()" in projected
    assert "in read_config" in projected and "in <module>" in projected
    assert "old/" not in projected and "2717" not in projected
    assert "config.getint" not in projected, "the echo of the source line must go"
    # A warning that names a line, including a Windows path with a drive letter.
    warning = r"C:\t\old\pycodestyle.py:2588: UserWarning: [pep8] is deprecated"
    assert canonicalise_stream(warning) == "pycodestyle.py: UserWarning: [pep8] is deprecated"
    # Anything that is not a traceback or a warning passes through untouched.
    assert canonicalise_stream("E501 line too long") == "E501 line too long"


def test_an_api_case_observes_a_surface_a_program_cannot_show() -> None:
    """`api` cases close the blind spot the stage-2 comparison found.

    The upstream suite caught `get_parser().add_option` disappearing; no CLI case
    could, because no run reaches that attribute. The snapshot is compared like
    any other value, so the difference shows up as a difference.
    """
    from harness.cases import Case

    case = Case(
        id="api-parser",
        source="api",
        entry="pycodestyle",
        args=(),
        kind="api",
        module="pycodestyle",
        call={"entry": "get_parser", "args": [], "kwargs": {}},
    )
    body = case.body()
    assert body["kind"] == "api" and body["module"] == "pycodestyle"
    assert body["call"]["entry"] == "get_parser"
    # An api case with no module is refused at load time, not at run time.
    path = REPO / "harness" / "cases" / "pycodestyle-cli.json"
    payload = __import__("json").loads(path.read_text(encoding="utf-8"))
    api = [entry for entry in payload["cases"] if entry.get("kind") == "api"]
    assert {entry["id"] for entry in api} == {"api-module", "api-parser"}
    assert all(entry["module"] == "pycodestyle" for entry in api)


def test_an_api_snapshot_does_not_contain_the_path_it_was_imported_from() -> None:
    """The no-op canary found this: two copies of the same tree differ by path.

    `pycodestyle.USER_CONFIG` is `expanduser('~/.config/pycodestyle')`, evaluated at
    import time, so it is the path of whichever copy the child imported. The canary
    compares a tree against a copy of that tree at a different path, and a comment
    mutant "differed" — in a constant nobody had touched. Every path a snapshot is
    allowed to see is now replaced by a marker.
    """
    from harness.child import _path_roots, _project_paths

    roots = _path_roots()
    text = f"{roots[0][0]}/pycodestyle.py:{roots[0][0]}/.config"
    projected = _project_paths(text, roots)
    assert roots[0][0] not in projected
    assert projected.startswith(roots[0][1])
    # Nested containers are visited; a non-string leaf is left alone.
    nested = _project_paths({"a": [roots[0][0], 7, {"b": roots[0][0]}]}, roots)
    assert nested["a"][0] == roots[0][1] and nested["a"][1] == 7
    assert nested["a"][2]["b"] == roots[0][1]
    # Longest root wins, so a root that is a prefix of another cannot half-rewrite it.
    pairs = [("/a", "<short>"), ("/a/b", "<long>")]
    assert _project_paths("/a/b/c", pairs) == "<long>/c"



def test_a_leftover_case_directory_is_an_error_not_an_unsupported_case(tmp_path: Any) -> None:
    """A stale directory must not silently make a case unobservable.

    The defect this pins: `rmtree(ignore_errors=True)` hid a locked directory, the
    following `mkdir` raised `FileExistsError`, and the outer handler recorded the
    case as `unsupported` — so a spinner left by a killed run turned `fixture-e25`
    and `fixture-e30not` into "the target was opaque" in every later run, forever,
    with nothing in the report saying otherwise.
    """
    from harness import child as child_module

    stuck = tmp_path / "case"
    stuck.mkdir()
    (stuck / "file.py").write_text("x = 1\n", encoding="utf-8")
    (stuck / "file.py").chmod(0o444)  # read-only: the Windows failure mode

    child_module._rmtree_hard(stuck)
    assert not stuck.exists(), "a read-only file must not block the removal"
    child_module._rmtree_hard(stuck)  # already gone: no error


def test_a_timed_out_cli_case_ends_its_whole_process_tree(tmp_path: Any) -> None:
    """`subprocess.run(timeout=)` kills one process; a case may leave a tree.

    Two orphans from a killed mutation pass each burned 3 500 s of CPU and held
    their case directories open, which is how the leftover-directory bug above was
    triggered in the first place. The grandchild writes its pid, so the assertion
    is about that process, not about the direct child.
    """
    import sys
    import time

    import psutil

    from harness import child as child_module

    pid_file = tmp_path / "grandchild.pid"
    script = tmp_path / "spinner.py"
    body = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(600)\n"
    )
    script.write_text(body, encoding="utf-8")

    original = child_module._CLI_TIMEOUT_SECONDS
    child_module._CLI_TIMEOUT_SECONDS = 5.0
    try:
        started = time.monotonic()
        exit_code, _out, err, signal_number = child_module._run_cli_process(
            [sys.executable, str(script)], cwd=tmp_path, env=dict(), stdin_text=""
        )
        elapsed = time.monotonic() - started
    finally:
        child_module._CLI_TIMEOUT_SECONDS = original

    assert exit_code is None and signal_number is None and err == "timeout"
    assert elapsed < 60, f"the timeout took {elapsed:.1f}s"
    grandchild = int(pid_file.read_text(encoding="utf-8"))
    assert not psutil.pid_exists(grandchild), (
        "the grandchild survived the case timeout; it would spin until the machine "
        "is rebooted and hold its case directory open"
    )


def test_a_case_dies_with_its_parent_even_when_the_parent_is_killed(tmp_path: Any) -> None:
    """The timeout is not the only way a case ends: the pass can be killed.

    A mutant that loops forever is stopped by the pass-wide CPU budget, which kills
    the harness process mid-case — no Python code runs afterwards, so no timeout
    cleanup either. Two such orphans each burned 3 500 s of CPU and kept their case
    directories locked, which is how a case turned into `unsupported` (§5.1 of the
    corrected report).

    The guarantee differs by platform and the test says so: Windows uses a job
    object with kill-on-job-close, which covers the whole tree; POSIX uses
    `PR_SET_PDEATHSIG`, which covers the process the harness spawned (a CLI case is
    pycodestyle, which spawns nothing).
    """
    import os
    import subprocess
    import sys
    import time

    import psutil

    repo = Path(__file__).resolve().parents[1]
    direct_pid = tmp_path / "direct.pid"
    grandchild_pid = tmp_path / "grandchild.pid"
    runner = tmp_path / "runner.py"
    runner.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(repo / 'src')!r})\n"
        "from pathlib import Path\n"
        "from harness import child\n"
        "spinner = Path(sys.argv[1])\n"
        "child._CLI_TIMEOUT_SECONDS = 600\n"
        "child._run_cli_process([sys.executable, str(spinner)], cwd=spinner.parent,\n"
        "                       env=dict(), stdin_text='')\n",
        encoding="utf-8",
    )
    spinner = tmp_path / "spinner.py"
    spinner.write_text(
        "import os, subprocess, sys, time\n"
        f"open({str(direct_pid)!r}, 'w').write(str(os.getpid()))\n"
        "kid = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
        f"open({str(grandchild_pid)!r}, 'w').write(str(kid.pid))\n"
        "time.sleep(600)\n",
        encoding="utf-8",
    )

    parent = subprocess.Popen([sys.executable, str(runner), str(spinner)])
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not grandchild_pid.exists():
        time.sleep(0.1)
    assert grandchild_pid.exists(), "the case never started"
    case_pid = int(direct_pid.read_text(encoding="utf-8"))
    grandchild = int(grandchild_pid.read_text(encoding="utf-8"))

    psutil.Process(parent.pid).kill()  # what the CPU budget does to the harness
    parent.wait(timeout=30)

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and psutil.pid_exists(case_pid):
        time.sleep(0.1)
    assert not psutil.pid_exists(case_pid), (
        "the case process outlived the harness process; it would spin and hold its "
        "case directory until the machine is rebooted"
    )

    if os.name == "nt":
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and psutil.pid_exists(grandchild):
            time.sleep(0.1)
        assert not psutil.pid_exists(grandchild), (
            "a Windows job object kills the whole tree, so the case's own child "
            "must be gone too"
        )
