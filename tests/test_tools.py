"""Typed tools (Tasks 4–5): reads, search, apply_patch via the wrapper,
allowlisted commands, budgets, error conversion, and the tool_calls log."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from engine import db, jobs, tools
from engine.sandbox import RunResult, SandboxLimits, SandboxSpec
from engine.tools import CommandAllowlist, Tool, ToolBudgets, ToolIO
from engine.workspace import (
    WorkspaceTree,
    decode_patch_json,
    sha256_hex,
)


def _tree(**files: str) -> WorkspaceTree:
    """Bare names map to `name + '.txt'` so `_tree(a='x')` pairs with edits
    on 'a.txt'; explicit names ('src/a.py') are used verbatim."""
    return WorkspaceTree(
        {
            (name if "." in name else f"{name}.txt"): value.encode("utf-8")
            for name, value in files.items()
        }
    )


def _ctx(tree: WorkspaceTree, **overrides: Any) -> tools.ToolContext:
    kwargs: dict[str, Any] = dict(
        workspace=tree,
        workspace_id=1,
        parent_state_hash=tree.state_hash(),
        job_id=uuid4(),
        attempt=1,
        run_ref="test-run",
    )
    kwargs.update(overrides)
    return tools.ToolContext(**kwargs)


def _run_result(**overrides: Any) -> RunResult:
    fields: dict[str, Any] = dict(
        job_id="tools-test",
        attempt=0,
        container_id=None,
        exit_code=0,
        timed_out=False,
        oom_killed=False,
        kill_failed=False,
        stdout=b"",
        stderr=b"",
        stdout_truncated=False,
        stderr_truncated=False,
        files={},
        symlinks_skipped=0,
        duration_seconds=0.05,
    )
    fields.update(overrides)
    return RunResult(**fields)


class RecordingRunner:
    def __init__(self, result: RunResult | None = None) -> None:
        self.result = result or _run_result()
        self.specs: list[SandboxSpec] = []
        self.limits: list[SandboxLimits] = []

    def __call__(self, spec: SandboxSpec, limits: SandboxLimits) -> RunResult:
        self.specs.append(spec)
        self.limits.append(limits)
        return self.result


_ALLOWLIST = CommandAllowlist(
    base_image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
    tests_argv=("python", "-m", "pytest", "{test_id}"),
    lint_argv=("python", "-m", "ruff", "check", "{path}"),
)


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


def test_read_line_ranges_one_based_inclusive() -> None:
    tree = _tree(f="a\nb\nc\nd\n")
    ctx = _ctx(tree)
    result = ctx.invoke("read_file", {"path": "f.txt", "start": 2, "end": 3})
    assert result.ok
    data = result.data
    assert data["total_lines"] == 4
    assert data["content"] == "b\nc\n"
    assert data["start_line"] == 2 and data["end_line"] == 3
    assert data["file_hash"] == sha256_hex(b"a\nb\nc\nd\n")
    assert data["truncated"] is False


def test_read_default_range_is_whole_file() -> None:
    tree = _tree(f="a\nb\nc\n")
    result = _ctx(tree).invoke("read_file", {"path": "f.txt"})
    assert result.data["content"] == "a\nb\nc\n"
    assert result.data["end_line"] == 3


def test_read_preserves_crlf_exactly() -> None:
    tree = WorkspaceTree({"f.txt": b"one\r\ntwo\r\n"})
    result = _ctx(tree).invoke("read_file", {"path": "f.txt"})
    assert result.data["content"] == "one\r\ntwo\r\n"  # \r stays attached


def test_read_counts_file_without_trailing_newline() -> None:
    tree = WorkspaceTree({"f.txt": b"a\nb"})
    result = _ctx(tree).invoke("read_file", {"path": "f.txt"})
    assert result.data["total_lines"] == 2
    assert result.data["content"] == "a\nb"


def test_read_empty_file() -> None:
    tree = WorkspaceTree({"f.txt": b""})
    result = _ctx(tree).invoke("read_file", {"path": "f.txt"})
    assert result.data["total_lines"] == 0
    assert result.data["content"] == ""


def test_read_start_beyond_eof() -> None:
    tree = _tree(f="a\nb\n")
    result = _ctx(tree).invoke("read_file", {"path": "f.txt", "start": 5})
    assert result.ok
    assert result.data["content"] == ""


def test_read_refuses_binary() -> None:
    tree = WorkspaceTree({"f.txt": b"prefix\x00binary"})
    result = _ctx(tree).invoke("read_file", {"path": "f.txt"})
    assert not result.ok
    assert result.error_code == "binary_file"


def test_read_not_found() -> None:
    result = _ctx(_tree()).invoke("read_file", {"path": "absent.txt"})
    assert result.error_code == "not_found"


def test_read_path_denied() -> None:
    result = _ctx(_tree(f="x\n")).invoke("read_file", {"path": "../escape"})
    assert result.error_code == "path_denied"


def test_read_end_before_start_is_invalid_args() -> None:
    tree = _tree(f="a\nb\n")
    result = _ctx(tree).invoke("read_file", {"path": "f.txt", "start": 2, "end": 1})
    assert result.error_code == "invalid_args"


def test_read_byte_cap_truncates() -> None:
    tree = WorkspaceTree({"f.txt": b"".join(b"x" * 1000 + b"\n" for _ in range(200))})  # ~200 KB
    result = _ctx(tree).invoke("read_file", {"path": "f.txt"})
    assert result.data["truncated"] is True
    assert len(result.data["content"].encode("utf-8")) <= tools.READ_MAX_BYTES


# ---------------------------------------------------------------------------
# search — literal
# ---------------------------------------------------------------------------


def test_search_literal_matches_with_lines() -> None:
    tree = _tree(**{"a.py": "alpha = 1\nbeta alpha = 2\n", "b.py": "no hits\n", "c.py": "alpha\n"})
    result = _ctx(tree).invoke("search", {"query": "alpha"})
    assert result.ok
    data = result.data
    assert len(data["matches"]) == 3
    assert data["matches"][0] == {"path": "a.py", "line": 1, "excerpt": "alpha = 1"}
    assert data["matches"][1]["line"] == 2
    assert data["matches"][2]["path"] == "c.py"
    assert data["files_searched"] == 3
    assert data["truncated"] is False


def test_search_is_case_sensitive() -> None:
    tree = _tree(f="Foo foo\n")
    result = _ctx(tree).invoke("search", {"query": "Foo"})
    assert len(result.data["matches"]) == 1


def test_search_truncates_at_max_results() -> None:
    tree = _tree(f="hit\nhit\nhit\n")
    result = _ctx(tree).invoke("search", {"query": "hit", "max_results": 2})
    assert len(result.data["matches"]) == 2
    assert result.data["truncated"] is True  # a third exists


def test_search_skips_binary_files() -> None:
    tree = WorkspaceTree({"bin": b"\x00hit", "text.py": b"hit\n"})
    result = _ctx(tree).invoke("search", {"query": "hit"})
    data = result.data
    assert len(data["matches"]) == 1
    assert data["matches"][0]["path"] == "text.py"
    assert data["binary_files_skipped"] == 1


def test_search_empty_query_rejected_by_model() -> None:
    result = _ctx(_tree(f="x\n")).invoke("search", {"query": ""})
    assert result.error_code == "invalid_args"


# ---------------------------------------------------------------------------
# apply_patch through the wrapper (proposed patch; tree updated; no append)
# ---------------------------------------------------------------------------


def test_apply_creates_and_edits_atomically() -> None:
    tree = _tree(a="hello world\n")
    ctx = _ctx(tree)
    result = ctx.invoke(
        "apply_patch",
        {
            "edits": [
                {
                    "path": "new.txt",
                    "expected_file_hash": None,
                    "old_string": "",
                    "new_string": "created",
                },
                {
                    "path": "a.txt",
                    "expected_file_hash": sha256_hex(b"hello world\n"),
                    "old_string": "world",
                    "new_string": "there",
                },
            ]
        },
    )
    assert result.ok, result.data
    data = result.data
    assert data["files_changed"] == ["a.txt", "new.txt"]
    # the tree already carries the edits
    assert tree.get("new.txt") == b"created"
    assert tree.get("a.txt") == b"hello there\n"
    # the proposed patch is durable-shaped and decodes to both edits
    parent, new, edits = decode_patch_json(data["patch_json"].encode("utf-8"))
    assert parent == ctx.parent_state_hash
    assert new == data["state_hash_after"] == tree.state_hash()
    assert len(edits) == 2
    assert data["state_hash_before"] == ctx.parent_state_hash


def test_apply_accumulates_the_step_patch() -> None:
    tree = _tree(a="v1\n")
    ctx = _ctx(tree)
    ctx.invoke(
        "apply_patch",
        {
            "edits": [
                {
                    "path": "a.txt",
                    "expected_file_hash": sha256_hex(b"v1\n"),
                    "old_string": "v1",
                    "new_string": "v2",
                }
            ]
        },
    )
    result2 = ctx.invoke(
        "apply_patch",
        {
            "edits": [
                {
                    "path": "a.txt",
                    "expected_file_hash": sha256_hex(b"v2\n"),
                    "old_string": "v2",
                    "new_string": "v3",
                }
            ]
        },
    )
    assert result2.ok
    # ONE pending patch with both edits: parent = step-start hash
    _, new, edits = decode_patch_json(result2.data["patch_json"].encode("utf-8"))
    assert len(edits) == 2
    assert new == tree.state_hash()
    assert ctx.pending_edits == edits


def test_apply_error_codes_through_wrapper() -> None:
    def code(args: dict[str, Any]) -> str | None:
        result = _ctx(_tree(a="dup dup\n", b="x\n")).invoke("apply_patch", args)
        return result.error_code

    assert (
        code(
            {
                "edits": [
                    {
                        "path": "missing.txt",
                        "expected_file_hash": sha256_hex(b"q"),
                        "old_string": "q",
                        "new_string": "w",
                    }
                ]
            }
        )
        == "stale_precondition"
    )
    assert (
        code(
            {
                "edits": [
                    {
                        "path": "a.txt",
                        "expected_file_hash": sha256_hex(b"dup dup\n"),
                        "old_string": "nope",
                        "new_string": "w",
                    }
                ]
            }
        )
        == "no_match"
    )
    assert (
        code(
            {
                "edits": [
                    {
                        "path": "a.txt",
                        "expected_file_hash": sha256_hex(b"dup dup\n"),
                        "old_string": "dup",
                        "new_string": "w",
                    }
                ]
            }
        )
        == "ambiguous_match"
    )
    assert (
        code({"edits": [{"path": "/abs", "expected_file_hash": None, "new_string": "w"}]})
        == "path_denied"
    )
    assert (
        code(
            {
                "edits": [
                    {
                        "path": "b.txt",
                        "expected_file_hash": sha256_hex(b"x\n"),
                        "old_string": "x",
                        "new_string": "y" * (4 * 1024 * 1024 + 1),
                    }
                ]
            }
        )
        == "too_large"
    )
    # create on an existing file
    assert (
        code({"edits": [{"path": "b.txt", "expected_file_hash": None, "new_string": "w"}]})
        == "stale_precondition"
    )


def test_apply_atomic_failure_leaves_tree_untouched() -> None:
    tree = _tree(a="aaa\n", b="bbb\n")
    ctx = _ctx(tree)
    result = ctx.invoke(
        "apply_patch",
        {
            "edits": [
                {
                    "path": "a.txt",
                    "expected_file_hash": sha256_hex(b"aaa\n"),
                    "old_string": "aaa",
                    "new_string": "zzz",
                },
                {
                    "path": "b.txt",
                    "expected_file_hash": sha256_hex(b"bbb\n"),
                    "old_string": "NOPE",
                    "new_string": "yyy",
                },
            ]
        },
    )
    assert result.error_code == "no_match"
    assert tree.get("a.txt") == b"aaa\n"  # the good edit did NOT land
    assert ctx.pending_edits == []


# ---------------------------------------------------------------------------
# run_tests / run_linter: allowlist + sandbox, mutations discarded
# ---------------------------------------------------------------------------


def test_run_tests_expands_test_id_and_uses_the_tree() -> None:
    tree = _tree(**{"tests/test_a.py": "def test_a():\n    assert True\n"})
    runner = RecordingRunner(_run_result(exit_code=0, stdout=b"1 passed\n"))
    ctx = _ctx(tree, sandbox_runner=runner, allowlist=_ALLOWLIST)
    result = ctx.invoke("run_tests", {"test_id": "tests/test_a.py::test_a"})
    assert result.ok
    spec = runner.specs[0]
    assert spec.argv == ("python", "-m", "pytest", "tests/test_a.py::test_a")
    assert spec.workspace_files == tree.files  # the whole tree, copied in
    assert spec.image.startswith("python@sha256:")
    assert result.data["exit_code"] == 0
    assert result.data["stdout"] == "1 passed\n"
    assert result.data["argv"] == ["python", "-m", "pytest", "tests/test_a.py::test_a"]


def test_run_tests_without_test_id_requires_it() -> None:
    runner = RecordingRunner()
    ctx = _ctx(_tree(), sandbox_runner=runner, allowlist=_ALLOWLIST)
    result = ctx.invoke("run_tests", {})
    assert result.error_code == "invalid_args"
    assert "required" in result.data["message"]
    assert runner.specs == []  # nothing ran


def test_run_linter_happy_path() -> None:
    tree = _tree(**{"a.py": "x = 1\n"})
    runner = RecordingRunner(_run_result(exit_code=1, stderr=b"1 error\n"))
    ctx = _ctx(tree, sandbox_runner=runner, allowlist=_ALLOWLIST)
    result = ctx.invoke("run_linter", {"path": "a.py"})
    assert result.ok
    assert runner.specs[0].argv == ("python", "-m", "ruff", "check", "a.py")
    assert result.data["exit_code"] == 1


def test_model_cannot_inject_flags() -> None:
    runner = RecordingRunner()
    ctx = _ctx(_tree(), sandbox_runner=runner, allowlist=_ALLOWLIST)
    # the classic: a test_id that is a flag
    result = ctx.invoke("run_tests", {"test_id": "-p"})
    assert result.error_code == "invalid_args"
    assert "flags" in result.data["message"]
    result = ctx.invoke("run_tests", {"test_id": "x --rootdir /"})
    assert result.error_code == "invalid_args"  # space is outside the charset
    # smuggled keys are refused by extra='forbid'
    result = ctx.invoke("run_linter", {"path": "a.py", "test_id": "tests/x.py"})
    assert result.error_code == "invalid_args"
    # traversal-shaped parameters are refused
    result = ctx.invoke("run_linter", {"path": "../etc"})
    assert result.error_code == "path_denied"
    assert runner.specs == []


def test_not_configured_errors() -> None:
    ctx = _ctx(_tree())  # no allowlist, no runner
    result = ctx.invoke("run_tests", {"test_id": "x"})
    assert result.error_code == "not_configured"
    partial = CommandAllowlist(base_image="python@sha256:0", tests_argv=("pytest", "{test_id}"))
    runner = RecordingRunner()
    ctx2 = _ctx(_tree(), sandbox_runner=runner, allowlist=partial)
    result = ctx2.invoke("run_linter", {"path": "a.py"})
    assert result.error_code == "not_configured"


def test_run_tests_sees_the_applied_edit() -> None:
    tree = _tree(**{"mod.py": "VALUE = 1\n"})
    runner = RecordingRunner()
    ctx = _ctx(tree, sandbox_runner=runner, allowlist=_ALLOWLIST)
    applied = ctx.invoke(
        "apply_patch",
        {
            "edits": [
                {
                    "path": "mod.py",
                    "expected_file_hash": sha256_hex(b"VALUE = 1\n"),
                    "old_string": "VALUE = 1",
                    "new_string": "VALUE = 2",
                }
            ]
        },
    )
    assert applied.ok
    ctx.invoke("run_tests", {"test_id": "tests/test_mod.py"})
    spec = runner.specs[0]
    assert spec.workspace_files["mod.py"] == b"VALUE = 2\n"  # the EDIT, copied in


def test_sandbox_mutations_are_discarded() -> None:
    tree = _tree(**{"mod.py": "VALUE = 1\n"})
    before_hash = tree.state_hash()
    runner = RecordingRunner(
        _run_result(exit_code=0, files={"mod.py": b"MUTATED\n", "junk.txt": b"x"})
    )
    ctx = _ctx(tree, sandbox_runner=runner, allowlist=_ALLOWLIST)
    result = ctx.invoke("run_tests", {"test_id": "tests/test_mod.py"})
    assert result.ok
    # the run's copied-out files are deliberately ignored
    assert "files" not in result.data
    assert tree.get("mod.py") == b"VALUE = 1\n"
    assert tree.get("junk.txt") is None
    assert tree.state_hash() == before_hash
    assert ctx.pending_edits == []


def test_timeout_and_oom_surface_in_output() -> None:
    runner = RecordingRunner(
        _run_result(exit_code=137, timed_out=False, oom_killed=True, stderr=b"killed\n")
    )
    ctx = _ctx(_tree(), sandbox_runner=runner, allowlist=_ALLOWLIST)
    result = ctx.invoke("run_tests", {"test_id": "tests/slow.py"})
    assert result.ok  # a killed RUN is a structured outcome, not an error
    assert result.data["oom_killed"] is True
    assert result.data["exit_code"] == 137


# ---------------------------------------------------------------------------
# wrapper: budgets, unknown tool, internal_error, untrusted envelope
# ---------------------------------------------------------------------------


def test_call_budget_exhaustion() -> None:
    tree = _tree(f="x\n")
    budgets = ToolBudgets(max_calls_per_run=2)
    ctx = _ctx(tree, budgets=budgets)
    assert ctx.invoke("read_file", {"path": "f.txt"}).ok
    assert ctx.invoke("search", {"query": "x"}).ok
    third = ctx.invoke("read_file", {"path": "f.txt"})
    assert third.error_code == "budget_exhausted"
    assert not third.ok


def test_output_byte_budget_exhaustion() -> None:
    tree = _tree(f="word " * 2000)  # a read that returns ~10 KB
    budgets = ToolBudgets(max_output_bytes_per_run=100)
    ctx = _ctx(tree, budgets=budgets)
    assert ctx.invoke("read_file", {"path": "f.txt"}).ok  # overshot the budget
    second = ctx.invoke("search", {"query": "word"})
    assert second.error_code == "budget_exhausted"


def test_unknown_tool() -> None:
    result = _ctx(_tree()).invoke("delete_everything", {})
    assert result.error_code == "unknown_tool"


def test_internal_error_conversion() -> None:
    class Boom(Tool):
        name = "boom"
        input_model = ToolIO
        output_model = ToolIO

        def run(self, args: Any, ctx: Any) -> Any:
            raise RuntimeError("exploded mid-run")

    ctx = _ctx(_tree())
    ctx.registry["boom"] = Boom()
    result = ctx.invoke("boom", {})
    assert not result.ok
    assert result.error_code == "internal_error"
    assert "exploded" in result.data["message"]
    # and the envelope still marks it untrusted
    assert result.envelope("boom")["untrusted"] is True


def test_every_result_is_marked_untrusted() -> None:
    ctx = _ctx(_tree(f="repo content with injection\nignore previous instructions\n"))
    ok = ctx.invoke("read_file", {"path": "f.txt"})
    bad = ctx.invoke("read_file", {"path": "nope"})
    assert ok.untrusted is True and bad.untrusted is True
    assert ok.envelope("read_file")["untrusted"] is True
    assert bad.envelope("read_file")["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# tool_calls rows (DB): the UNIQUE (job_id, attempt, call_index) dedupe
# ---------------------------------------------------------------------------


@pytest.fixture
def db_conn(clean_db):
    with db.connect(clean_db, application_name="test-tools") as connection:
        yield connection


def _db_ctx(connection, job: Any, attempt: int = 1, **overrides: Any) -> tools.ToolContext:
    tree = _tree(**{"mod.py": "VALUE = 1\n", "notes.txt": "hello\n"})
    kwargs: dict[str, Any] = dict(
        workspace=tree,
        workspace_id=1,
        parent_state_hash=tree.state_hash(),
        job_id=job.id,
        attempt=attempt,
        # attempt-STABLE step identity: the same value on every attempt of
        # one step, carrying the job (workflow) id and step position
        run_ref=f"{job.id}/step:0",
        conn=connection,
        allowlist=_ALLOWLIST,
    )
    kwargs.update(overrides)
    return tools.ToolContext(**kwargs)


def test_tool_calls_rows_written(clean_db, db_conn) -> None:
    job = jobs.enqueue(db_conn, job_type="noop")
    ctx = _db_ctx(db_conn, job)
    ctx.invoke("read_file", {"path": "notes.txt"})
    ctx.invoke("read_file", {"path": "../escape"})  # refused: raw args logged
    ctx.invoke(
        "apply_patch",
        {
            "edits": [
                {
                    "path": "mod.py",
                    "expected_file_hash": sha256_hex(b"VALUE = 1\n"),
                    "old_string": "1",
                    "new_string": "x" * 300,  # big enough to exceed the preview
                }
            ]
        },
    )
    rows = db_conn.execute(
        "SELECT call_index, tool, ok, error_code, args_sha256, args_bytes,"
        " args_preview, output_bytes, duration_ms"
        " FROM tool_calls WHERE job_id = %s ORDER BY call_index",
        (job.id,),
    ).fetchall()
    assert [r["call_index"] for r in rows] == [0, 1, 2]
    assert [r["tool"] for r in rows] == ["read_file", "read_file", "apply_patch"]
    assert [r["ok"] for r in rows] == [True, False, True]
    assert rows[1]["error_code"] == "path_denied"
    # the canonical JSON of the VALIDATED ReadFileInput (ensure_ascii,
    # default separators — the same bytes the digest is taken over)
    canonical = '{"path": "notes.txt", "start": 1, "end": null}'
    assert rows[0]["args_sha256"] == sha256_hex(canonical.encode("utf-8"))
    assert rows[0]["args_bytes"] == len(canonical.encode("utf-8"))
    assert rows[0]["args_preview"] == canonical
    # apply_patch args are summarized too: the FULL edits are NOT in the row
    assert rows[2]["args_preview"].startswith('{"edits": [')
    assert len(rows[2]["args_preview"]) == 200  # truncated at 200 chars
    assert "x" * 300 not in rows[2]["args_preview"]  # the repo content is not there
    assert rows[2]["args_bytes"] > 200  # the full args were bigger than the preview
    assert all(r["output_bytes"] > 0 for r in (rows[0], rows[2]))
    assert all(r["duration_ms"] >= 0 for r in rows)


def test_redelivered_step_does_not_duplicate_rows(clean_db, db_conn) -> None:
    job = jobs.enqueue(db_conn, job_type="noop")
    # attempt 1 runs two calls
    ctx1 = _db_ctx(db_conn, job, attempt=1)
    ctx1.invoke("read_file", {"path": "notes.txt"})
    ctx1.invoke("search", {"query": "VALUE"})
    # the step is redelivered (attempt 2): call_index restarts — new rows
    ctx2 = _db_ctx(db_conn, job, attempt=2)
    ctx2.invoke("read_file", {"path": "notes.txt"})
    # SAME attempt re-executed from index 0 (e.g. a crash mid-step): the
    # UNIQUE constraint keeps the FIRST record, no duplicates
    ctx3 = _db_ctx(db_conn, job, attempt=1)
    ctx3.invoke("read_file", {"path": "notes.txt"})
    ctx3.invoke("search", {"query": "VALUE"})
    rows = db_conn.execute(
        "SELECT attempt, call_index, count(*) AS n FROM tool_calls"
        " WHERE job_id = %s GROUP BY attempt, call_index ORDER BY attempt, call_index",
        (job.id,),
    ).fetchall()
    assert len(rows) == 3  # not 5: the same-attempt rerun deduped
    assert all(int(r["n"]) == 1 for r in rows)


def test_nul_in_args_is_sanitized_before_hashing(clean_db, db_conn) -> None:
    job = jobs.enqueue(db_conn, job_type="noop")
    ctx = _db_ctx(db_conn, job)
    result = ctx.invoke("read_file", {"path": "bad\x00path"})
    assert result.error_code == "path_denied"
    row = db_conn.execute(
        "SELECT args_sha256, args_preview FROM tool_calls WHERE job_id = %s AND call_index = 0",
        (job.id,),
    ).fetchone()
    assert row is not None  # the row WAS written
    assert "\x00" not in row["args_preview"]  # no NUL anywhere in the row
    # the sanitized U+FFFD is stored as the JSON escape \ufffd (ensure_ascii)
    assert "\\ufffd" in row["args_preview"]
    # the digest is over the SANITIZED canonical JSON, so it is reproducible
    canonical = '{"path": "bad\\ufffdpath"}'
    assert row["args_sha256"] == sha256_hex(canonical.encode("utf-8"))


def test_row_failure_never_breaks_the_tool_result(clean_db) -> None:
    job = jobs.enqueue(db.connect(clean_db), job_type="noop")
    ctx = _db_ctx(db.connect(clean_db), job, conn=None)
    result = ctx.invoke("read_file", {"path": "notes.txt"})
    assert result.ok  # conn=None only skips logging


def test_budget_refusals_are_logged_too(clean_db, db_conn) -> None:
    job = jobs.enqueue(db_conn, job_type="noop")
    ctx = _db_ctx(db_conn, job, budgets=ToolBudgets(max_calls_per_run=1))
    ctx.invoke("read_file", {"path": "notes.txt"})
    ctx.invoke("read_file", {"path": "notes.txt"})
    rows = db_conn.execute(
        "SELECT ok, error_code FROM tool_calls WHERE job_id = %s ORDER BY call_index",
        (job.id,),
    ).fetchall()
    assert len(rows) == 2
    assert rows[1]["error_code"] == "budget_exhausted"
