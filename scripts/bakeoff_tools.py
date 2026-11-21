"""The tool layer the bake-off runs models against.

These are the real mechanisms, not a mock: a persistent workspace, reads that
return a content hash, patches that refuse a stale precondition or a text that
does not occur exactly once, and a test command whose result is the target
project's own pytest exit status.

The layer is deliberately strict — a model cannot paper over a mistake by
retrying blindly — because the point of the exercise is to measure the loop, and
the loop's failure modes are exactly these three: a stale hash, a text that is not
there, and a test that still fails.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "model_bakeoff"

MAX_READ_LINES = 200
MAX_SEARCH_MATCHES = 50


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class ToolResult:
    payload: dict[str, Any]
    is_error: bool = False

    def to_message(self) -> str:
        return json.dumps(self.payload, ensure_ascii=False, sort_keys=True)


@dataclass
class Workspace:
    """A scratch copy of the fixture repository."""

    root: Path
    force_stale_once: bool = False
    _forced_stale_done: bool = field(default=False, repr=False)

    @classmethod
    def fresh(cls, destination: Path, force_stale_once: bool = False) -> Workspace:
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(
            FIXTURE_ROOT,
            destination,
            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "conftest.py"),
        )
        # A pytest config of its own: without it the repository's `pyproject.toml`
        # is found by walking upwards and `testpaths` points at the harness suite.
        (destination / "pytest.ini").write_text(
            "\n".join(["[pytest]", "testpaths = tests"]), encoding="utf-8"
        )
        return cls(root=destination, force_stale_once=force_stale_once)

    # -- path safety -------------------------------------------------------
    def _resolve(self, raw: str) -> Path | None:
        candidate = Path(raw)
        if candidate.is_absolute() or ".." in candidate.parts:
            return None
        resolved = (self.root / candidate).resolve()
        try:
            resolved.relative_to(self.root.resolve())
        except ValueError:
            return None
        return resolved

    def read_text(self, raw: str) -> str | None:
        path = self._resolve(raw)
        if path is None or not path.is_file():
            return None
        return path.read_text(encoding="utf-8", errors="replace")

    def write_text(self, raw: str, text: str) -> bool:
        path = self._resolve(raw)
        if path is None:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return True

    # -- tools -------------------------------------------------------------
    def read_file(self, path: str, start: int = 1, end: int | None = None) -> ToolResult:
        text = self.read_text(path)
        if text is None:
            return ToolResult({"error": "no_such_file", "path": path}, is_error=True)
        lines = text.splitlines()
        first = max(1, int(start or 1))
        last = len(lines) if end is None else min(len(lines), int(end))
        if last < first:
            return ToolResult(
                {"error": "empty_range", "start": first, "end": last, "total_lines": len(lines)},
                is_error=True,
            )
        window = [
            {"n": number, "text": lines[number - 1]}
            for number in range(first, min(last, first + MAX_READ_LINES - 1) + 1)
        ]
        return ToolResult(
            {
                "path": path,
                "sha256": sha256_text(text),
                "total_lines": len(lines),
                "start": first,
                "end": window[-1]["n"] if window else first,
                "lines": window,
                "truncated": last > first + MAX_READ_LINES - 1,
            }
        )

    def search(self, pattern: str, path: str = ".") -> ToolResult:
        base = self._resolve(path)
        if base is None:
            return ToolResult({"error": "path_outside_workspace", "path": path}, is_error=True)
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return ToolResult({"error": "bad_pattern", "detail": str(exc)}, is_error=True)
        matches: list[dict[str, Any]] = []
        files = [base] if base.is_file() else sorted(
            item for item in base.rglob("*") if item.is_file()
            and "__pycache__" not in item.parts
        )
        for item in files:
            try:
                text = item.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matches.append(
                        {
                            "path": str(item.relative_to(self.root)).replace(os.sep, "/"),
                            "line": number,
                            "text": line[:200],
                        }
                    )
                    if len(matches) >= MAX_SEARCH_MATCHES:
                        break
            if len(matches) >= MAX_SEARCH_MATCHES:
                break
        if not matches:
            return ToolResult(
            {"error": "no_match", "pattern": pattern, "path": path}, is_error=True
        )
        return ToolResult(
            {"pattern": pattern, "path": path, "count": len(matches), "matches": matches}
        )

    def list_dir(self, path: str = ".") -> ToolResult:
        base = self._resolve(path)
        if base is None or not base.is_dir():
            return ToolResult({"error": "no_such_directory", "path": path}, is_error=True)
        entries = []
        for item in sorted(base.iterdir()):
            if item.name == "__pycache__":
                continue
            entries.append(
                {
                    "name": item.name,
                    "type": "dir" if item.is_dir() else "file",
                    "bytes": item.stat().st_size if item.is_file() else None,
                }
            )
        return ToolResult({"path": path, "entries": entries})

    def apply_patch(self, path: str, old: str, new: str, precondition_sha: str) -> ToolResult:
        text = self.read_text(path)
        if text is None:
            return ToolResult({"error": "no_such_file", "path": path}, is_error=True)
        current = sha256_text(text)
        if self.force_stale_once and not self._forced_stale_done:
            # The task is about recovering from this, so it happens exactly once.
            self._forced_stale_done = True
            return ToolResult(
                {
                    "error": "stale_precondition",
                    "path": path,
                    "current_sha": current,
                    "hint": "re-read the file and patch again with the current sha256",
                },
                is_error=True,
            )
        if precondition_sha != current:
            return ToolResult(
                {
                    "error": "stale_precondition",
                    "path": path,
                    "current_sha": current,
                    "hint": "re-read the file and patch again with the current sha256",
                },
                is_error=True,
            )
        occurrences = text.count(old) if old else 0
        if occurrences == 0:
            return ToolResult(
                {
                    "error": "no_match",
                    "path": path,
                    "hint": "read_file the range you want to change and copy the text exactly",
                },
                is_error=True,
            )
        if occurrences > 1:
            return ToolResult(
                {"error": "ambiguous_match", "count": occurrences, "path": path}, is_error=True
            )
        updated = text.replace(old, new, 1)
        self.write_text(path, updated)
        return ToolResult(
            {
                "applied": True,
                "path": path,
                "sha256": sha256_text(updated),
                "lines_added": max(0, updated.count("\n") - text.count("\n")),
                "lines_removed": max(0, text.count("\n") - updated.count("\n")),
            }
        )

    def run_tests(self, timeout_s: float = 120.0) -> ToolResult:
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            "pytest.ini",
            "tests",
            "-q",
            "-p",
            "no:cacheprovider",
        ]
        env = {
            key: value
            for key, value in os.environ.items()
            if key in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "LANG")
        }
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            completed = subprocess.run(
                command,
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return ToolResult({"error": "test_timeout", "timeout_s": timeout_s}, is_error=True)
        output = (completed.stdout + completed.stderr).strip()
        tail = output.splitlines()[-15:]
        summary = ""
        for line in reversed(tail):
            if re.search(r"\b(passed|failed|error)\b", line):
                summary = line.strip()
                break
        return ToolResult(
            {
                "command": "python -m pytest -q",
                "exit_code": completed.returncode,
                "summary": summary,
                "output_tail": tail,
            },
            is_error=completed.returncode != 0,
        )

    def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if name == "read_file":
            return self.read_file(
                str(arguments.get("path", "")),
                start=int(arguments.get("start") or 1),
                end=None if arguments.get("end") is None else int(arguments["end"]),
            )
        if name == "search":
            return self.search(str(arguments.get("pattern", "")), str(arguments.get("path") or "."))
        if name == "list_dir":
            return self.list_dir(str(arguments.get("path") or "."))
        if name == "apply_patch":
            return self.apply_patch(
                str(arguments.get("path", "")),
                str(arguments.get("old", "")),
                str(arguments.get("new", "")),
                str(arguments.get("precondition_sha", "")),
            )
        if name == "run_tests":
            return self.run_tests()
        return ToolResult({"error": "unknown_tool", "name": name}, is_error=True)


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read lines of a UTF-8 text file. Returns the file's sha256.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path relative to the workspace root.",
                    },
                    "start": {
                        "type": "integer",
                        "description": "First line, 1-based. Default 1.",
                    },
                    "end": {
                        "type": "integer",
                        "description": "Last line, inclusive. Default end of file.",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": (
                "Search a Python regular expression across files. Returns path and line."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Python regular expression."},
                    "path": {
                        "type": "string",
                        "description": "File or directory to search. Default '.'.",
                    },
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List a directory's entries with names, types and sizes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory. Default '.'."}
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": (
                "Replace one exact occurrence of `old` with `new` in a file. Requires the "
                "file's current sha256 from read_file; a stale hash is refused."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {
                        "type": "string",
                        "description": "Exact text to replace, including indentation.",
                    },
                    "new": {"type": "string", "description": "Replacement text."},
                    "precondition_sha": {
                        "type": "string",
                        "description": "sha256 of the file's current content.",
                    },
                },
                "required": ["path", "old", "new", "precondition_sha"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": (
                "Run the repository's test suite and return the exit code and output tail."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]

TOOL_ARGUMENTS: dict[str, dict[str, str]] = {
    "read_file": {"path": "str", "start": "int", "end": "int"},
    "search": {"pattern": "str", "path": "str"},
    "list_dir": {"path": "str"},
    "apply_patch": {"path": "str", "old": "str", "new": "str", "precondition_sha": "str"},
    "run_tests": {},
}

REQUIRED_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "read_file": ("path",),
    "search": ("pattern",),
    "list_dir": (),
    "apply_patch": ("path", "old", "new", "precondition_sha"),
    "run_tests": (),
}


def validate_arguments(name: str, arguments: dict[str, Any] | None) -> str:
    """Return "" when the call is well formed, otherwise the reason it is not.

    This is the validity measure behind "valid tool-call rate": a schema-shaped
    check, not a check that the call was the *right* call. Being wrong is a task
    failure; being malformed is a broken loop, and the two are counted apart.
    """
    if arguments is None:
        return "arguments are not a JSON object"
    if name not in TOOL_ARGUMENTS:
        return f"unknown tool {name!r}"
    allowed = TOOL_ARGUMENTS[name]
    for key, value in arguments.items():
        if key not in allowed:
            return f"unexpected argument {key!r}"
        wanted = allowed[key]
        if wanted == "str" and not isinstance(value, str):
            return f"{key} must be a string"
        if wanted == "int" and not isinstance(value, int):
            return f"{key} must be an integer"
    for key in REQUIRED_ARGUMENTS[name]:
        if key not in arguments:
            return f"missing required argument {key!r}"
    path = arguments.get("path")
    if isinstance(path, str) and (path.startswith("/") or ".." in Path(path).parts):
        return "path escapes the workspace"
    return ""


def run_check(
    workspace: Workspace, code: str, answer: str, timeout_s: float = 120.0
) -> tuple[bool, str]:
    """Run a task's check snippet inside the workspace.

    Exit 0 means the task succeeded. The snippet may print a reason; the last
    non-empty stdout line is kept as the explanation in the report.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if key in ("PATH", "SYSTEMROOT", "TEMP", "TMP")
    }
    env["BAKEOFF_ANSWER"] = answer
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=workspace.root,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return False, f"check timed out after {timeout_s}s"
    detail = ""
    lines = [
        line
        for line in (completed.stdout + completed.stderr).strip().splitlines()
        if line.strip()
    ]
    if lines:
        detail = lines[-1][:300]
    return completed.returncode == 0, detail
