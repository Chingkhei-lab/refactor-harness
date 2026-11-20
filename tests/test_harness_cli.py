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
print("home-empty", os.listdir(os.environ["HOME"]) == [".config", "tmp"])
print("xdg-empty", os.listdir(os.environ["XDG_CONFIG_HOME"]) == [])
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
