"""Typed tools (checkpoint 8, Tasks 4–5): pydantic models, a registry, a
context with budgets, and the wrapper that logs every call.

Boundary rules:

- Tools operate on the in-memory WorkspaceTree — never on a host
  filesystem (ADR 003 D8). The only host-adjacent surface is the sandbox,
  reached through an injected runner callable; this module imports no
  docker and touches no host path.
- Every tool output is UNTRUSTED DATA: it embeds repository content, and
  repository content can contain prompt injection (Phase 5 feeds it to a
  model). ToolResult.untrusted is always True and travels with the
  payload in the envelope.
- The wrapper is the only path a tool call takes: validation, budgets,
  timing, exception-to-internal_error conversion, and the tool_calls row
  (UNIQUE (job_id, attempt, call_index), ON CONFLICT DO NOTHING so a
  retried step cannot duplicate rows).

Error-code vocabulary:
- patch domain (from workspace.PatchError): stale_precondition, no_match,
  ambiguous_match, path_denied, too_large, binary_file
- read/search domain: not_found, binary_file, path_denied
- wrapper domain: invalid_args (model input failed validation),
  unknown_tool, budget_exhausted, not_configured (operator misconfig:
  no allowlist / no runner), internal_error (the catch-all)
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .paths import PathDenied, validate_path
from .sandbox import RunResult, SandboxLimits, SandboxSpec
from .workspace import (
    MAX_EDITS_PER_CALL,
    PATCH_MAX_BYTES,
    PatchEdit,
    PatchError,
    WorkspaceTree,
    canonical_patch_json,
    sha256_hex,
)

_SQL_DIR = Path(__file__).parent / "sql"


def _load_sql(name: str) -> str:
    return (_SQL_DIR / name).read_text(encoding="utf-8")


_TOOL_CALL_INSERT_SQL = _load_sql("tool_call_insert.sql")

READ_MAX_BYTES = 64 * 1024  # per-read content cap (returned bytes)
_BINARY_SNIFF_BYTES = 8000  # git's rule: NUL in the first 8 KiB => binary
_EXCERPT_MAX_CHARS = 240
_ARGS_PREVIEW_CHARS = 200  # tool_calls.args_preview cap (chars)
_ERROR_MESSAGE_MAX_CHARS = 500  # tool_calls.error_message cap (chars)


class ToolError(Exception):
    """A tool-level failure with a stable code, surfaced to the model."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# IO models — extra='forbid' everywhere: an unknown key is a model error,
# not silently ignored data.
# ---------------------------------------------------------------------------


class ToolIO(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadFileInput(ToolIO):
    path: str
    start: int = Field(default=1, ge=1)  # 1-based inclusive
    end: int | None = Field(default=None, ge=1)  # inclusive; None = to EOF


class ReadFileOutput(ToolIO):
    path: str
    file_hash: str
    total_lines: int
    start_line: int
    end_line: int
    content: str
    truncated: bool


class SearchMatch(ToolIO):
    path: str
    line: int
    excerpt: str


class SearchInput(ToolIO):
    query: str = Field(min_length=1, max_length=256)
    max_results: int = Field(default=20, ge=1, le=100)


class SearchOutput(ToolIO):
    query: str
    matches: list[SearchMatch]
    files_searched: int
    binary_files_skipped: int
    truncated: bool


class ApplyPatchInput(ToolIO):
    edits: list[PatchEdit] = Field(min_length=1, max_length=MAX_EDITS_PER_CALL)


class ApplyPatchOutput(ToolIO):
    state_hash_before: str
    state_hash_after: str
    files_changed: list[str]
    patch_json: str  # the canonical durable patch (proposed; append is the advance txn's job)


class RunTestsInput(ToolIO):
    test_id: str | None = None
    path: str | None = None


class RunLinterInput(ToolIO):
    path: str | None = None


class CommandOutput(ToolIO):
    argv: list[str]
    exit_code: int
    timed_out: bool
    oom_killed: bool
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    duration_seconds: float


# ---------------------------------------------------------------------------
# Command allowlist: the ONLY way run_tests/run_linter get an argv. The
# operator's config holds the template (with {test_id}/{path} placeholders);
# the model supplies only validated parameter values and can never add
# flags — extra='forbid' on the input, and the template is expanded into an
# argv LIST (no shell), so there is nothing to inject.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandAllowlist:
    base_image: str  # pinned by digest
    tests_argv: tuple[str, ...] | None = None
    lint_argv: tuple[str, ...] | None = None
    wall_seconds: float = 120.0
    mem_mb: int = 512

    def template(self, kind: str) -> tuple[str, ...] | None:
        if kind == "tests":
            return self.tests_argv
        if kind == "lint":
            return self.lint_argv
        raise ValueError(f"unknown command kind {kind!r}")


_PARAM_TOKEN_RE = re.compile(r"^[A-Za-z0-9_./:@+-]{1,200}$")


def _validate_token(value: str) -> str:
    """A test-id / path parameter: no flags, no traversal, no shell noise."""
    if value.startswith("-"):
        raise ToolError("invalid_args", "parameters cannot start with '-' (no flags)")
    if ".." in value:
        raise ToolError("invalid_args", "parameter contains '..'")
    if not _PARAM_TOKEN_RE.match(value):
        raise ToolError(
            "invalid_args",
            "parameter characters outside [A-Za-z0-9_./:@+-] or over 200 chars",
        )
    return value


def _validate_path_param(value: str) -> str:
    # path semantics FIRST: '..' and friends are path_denied, not a token error
    try:
        validated = validate_path(value)
    except PathDenied as exc:
        raise ToolError("path_denied", str(exc)) from exc
    return _validate_token(validated)


def _expand_argv(
    template: tuple[str, ...],
    provided: dict[str, str | None],
    validators: dict[str, Callable[[str], str]],
) -> list[str]:
    """Expand {placeholder} args; reject provided-but-unused parameters."""
    used: set[str] = set()
    argv: list[str] = []
    for arg in template:
        if len(arg) > 2 and arg.startswith("{") and arg.endswith("}"):
            key = arg[1:-1]
            value = provided.get(key)
            if value is None:
                raise ToolError("invalid_args", f"parameter {key!r} is required by this command")
            argv.append(validators[key](value))
            used.add(key)
        else:
            argv.append(arg)
    smuggled = sorted(k for k, v in provided.items() if v is not None and k not in used)
    if smuggled:
        raise ToolError("invalid_args", f"parameters not accepted by this command: {smuggled}")
    return argv


# ---------------------------------------------------------------------------
# Context, budgets, runner protocol
# ---------------------------------------------------------------------------


class SandboxRunner(Protocol):
    """The injected sandbox call: engine code stays docker-free."""

    def __call__(self, spec: SandboxSpec, limits: SandboxLimits) -> RunResult: ...


@dataclass
class ToolBudgets:
    """Per-run budgets, enforced by the wrapper."""

    max_calls_per_run: int = 200
    max_output_bytes_per_run: int = 2 * 1024 * 1024


@dataclass
class ToolResult:
    """What a tool call returned, post-wrapper. `data` is the validated
    output model's dump on success, or the error detail on failure."""

    ok: bool
    data: dict[str, Any]
    error_code: str | None
    duration_seconds: float
    output_bytes: int
    untrusted: bool = True

    def envelope(self, tool_name: str) -> dict[str, Any]:
        """The model-facing shape: the untrusted marker is IN the structure,
        so downstream code cannot format tool output without it."""
        payload: dict[str, Any] = {"tool": tool_name, "untrusted": True, "ok": self.ok}
        if self.ok:
            payload["result"] = self.data
        else:
            payload["error"] = {"code": self.error_code, "detail": self.data}
        return payload


class Tool:
    """Base class: name + validated input/output models; run is pure-ish."""

    name: ClassVar[str]
    input_model: ClassVar[type[BaseModel]]
    output_model: ClassVar[type[BaseModel]]

    def run(self, args: Any, ctx: ToolContext) -> BaseModel:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


def _split_lines(content: bytes) -> list[bytes]:
    """POSIX lines: boundaries are \\n only (\\r stays attached, so CRLF
    round-trips exactly); a missing final newline still counts as a line."""
    if not content:
        return []
    parts = content.split(b"\n")
    lines = [part + b"\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


class ReadFile(Tool):
    name = "read_file"
    input_model = ReadFileInput
    output_model = ReadFileOutput

    def run(self, args: ReadFileInput, ctx: ToolContext) -> ReadFileOutput:
        try:
            path = validate_path(args.path)
        except PathDenied as exc:
            raise ToolError("path_denied", str(exc)) from exc
        content = ctx.workspace.get(path)
        if content is None:
            raise ToolError("not_found", f"{path!r} is not in the workspace")
        if b"\x00" in content[:_BINARY_SNIFF_BYTES]:
            raise ToolError(
                "binary_file",
                f"{path!r} looks binary (NUL byte within the first {_BINARY_SNIFF_BYTES} bytes)",
            )
        lines = _split_lines(content)
        total = len(lines)
        start = args.start
        if args.end is not None and args.end < start:
            raise ToolError("invalid_args", f"end {args.end} is before start {start}")
        end = args.end if args.end is not None else total
        end = min(end, total)
        selected = lines[start - 1 : end] if start <= total else []
        out = bytearray()
        truncated = False
        included = 0
        for piece in selected:
            if len(out) + len(piece) > READ_MAX_BYTES:
                truncated = True
                break
            out.extend(piece)
            included += 1
        try:
            text = bytes(out).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ToolError("binary_file", f"{path!r} is not UTF-8 text: {exc}") from exc
        return ReadFileOutput(
            path=path,
            file_hash=sha256_hex(content),
            total_lines=total,
            start_line=start,
            end_line=start + included - 1 if included else start - 1,
            content=text,
            truncated=truncated,
        )


# ---------------------------------------------------------------------------
# search — LITERAL matching, on purpose
# ---------------------------------------------------------------------------


class Search(Tool):
    name = "search"
    input_model = SearchInput
    output_model = SearchOutput

    def run(self, args: SearchInput, ctx: ToolContext) -> SearchOutput:
        # Literal matching is a decision, not a limitation (recorded in
        # ADR 003 D8): str.find has no backtracking, so there is no ReDoS
        # shape — no pathological pattern can pin the worker CPU or starve
        # the heartbeat thread. RE2 was rejected (a C extension in the
        # runtime image for a need the run phase covers better); regex-in-
        # sandbox was rejected (a container spin-up per search).
        query = args.query
        matches: list[SearchMatch] = []
        files_searched = 0
        binary_skipped = 0
        truncated = False
        for path, content in sorted(ctx.workspace.files.items()):
            if b"\x00" in content[:_BINARY_SNIFF_BYTES]:
                binary_skipped += 1
                continue
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                binary_skipped += 1  # not UTF-8: not text-searchable here
                continue
            files_searched += 1
            idx = text.find(query)
            while idx != -1 and not truncated:
                line_start = text.rfind("\n", 0, idx) + 1
                line_end = text.find("\n", idx)
                if line_end == -1:
                    line_end = len(text)
                excerpt = text[line_start:line_end].rstrip("\r")[:_EXCERPT_MAX_CHARS]
                matches.append(
                    SearchMatch(path=path, line=text.count("\n", 0, idx) + 1, excerpt=excerpt)
                )
                if len(matches) >= args.max_results:
                    truncated = True  # more exist; cap hit
                    break
                idx = text.find(query, idx + len(query))
        return SearchOutput(
            query=query,
            matches=matches,
            files_searched=files_searched,
            binary_files_skipped=binary_skipped,
            truncated=truncated,
        )


# ---------------------------------------------------------------------------
# apply_patch — proposed only; the advance transaction appends it
# ---------------------------------------------------------------------------


class ApplyPatch(Tool):
    name = "apply_patch"
    input_model = ApplyPatchInput
    output_model = ApplyPatchOutput

    def run(self, args: ApplyPatchInput, ctx: ToolContext) -> ApplyPatchOutput:
        before = ctx.workspace.state_hash()
        try:
            # plan ALL edits first: atomic across files — one failure,
            # nothing applied
            plan = ctx.workspace.plan_edits(args.edits)
            all_edits = [*ctx.pending_edits, *args.edits]
            try:
                patch = canonical_patch_json(ctx.parent_state_hash, plan.new_state_hash, all_edits)
            except PatchError as exc:
                # the step's accumulated patch would exceed the cap: refuse
                # BEFORE committing (the tree is untouched by plan)
                raise ToolError(exc.code, exc.args[0]) from exc
            ctx.workspace.commit_plan(plan)
            ctx.pending_edits[:] = all_edits
        except PatchError as exc:
            raise ToolError(exc.code, exc.args[0]) from exc
        return ApplyPatchOutput(
            state_hash_before=before,
            state_hash_after=plan.new_state_hash,
            files_changed=sorted({edit.path for edit in args.edits}),
            patch_json=patch.decode("utf-8"),
        )


# ---------------------------------------------------------------------------
# run_tests / run_linter — allowlisted commands in the sandbox
# ---------------------------------------------------------------------------


class RunTests(Tool):
    name = "run_tests"
    input_model = RunTestsInput
    output_model = CommandOutput

    def run(self, args: RunTestsInput, ctx: ToolContext) -> CommandOutput:
        return _run_allowlisted(
            ctx,
            "tests",
            provided={"test_id": args.test_id, "path": args.path},
            validators={"test_id": _validate_token, "path": _validate_path_param},
        )


class RunLinter(Tool):
    name = "run_linter"
    input_model = RunLinterInput
    output_model = CommandOutput

    def run(self, args: RunLinterInput, ctx: ToolContext) -> CommandOutput:
        return _run_allowlisted(
            ctx,
            "lint",
            provided={"path": args.path},
            validators={"path": _validate_path_param},
        )


def _run_allowlisted(
    ctx: ToolContext,
    kind: str,
    *,
    provided: dict[str, str | None],
    validators: dict[str, Callable[[str], str]],
) -> CommandOutput:
    allowlist = ctx.allowlist
    if allowlist is None:
        raise ToolError("not_configured", "no command allowlist configured for this run")
    template = allowlist.template(kind)
    if template is None:
        raise ToolError("not_configured", f"no {kind} command configured for this workspace")
    argv = _expand_argv(template, provided, validators)
    if ctx.sandbox_runner is None:
        raise ToolError("not_configured", "no sandbox runner available for this run")
    spec = SandboxSpec(
        image=allowlist.base_image,
        argv=tuple(argv),
        env={"HOME": "/tmp"},
        workspace_files=ctx.workspace.files,  # the tree, copied in; nothing else
        run_key=os.urandom(6).hex(),
        job_id=f"tools-{ctx.job_id}",
        attempt=ctx.attempt,
    )
    limits = SandboxLimits(wall_seconds=allowlist.wall_seconds, mem_mb=allowlist.mem_mb)
    result = ctx.sandbox_runner(spec, limits)
    # The run's copied-out files are DELIBERATELY ignored (Task 4): sandbox
    # mutations are discarded — only the patch log changes the workspace.
    # A test asserts the tool result carries none of them.
    return CommandOutput(
        argv=argv,
        exit_code=result.exit_code,
        timed_out=result.timed_out,
        oom_killed=result.oom_killed,
        stdout=result.stdout.decode("utf-8", errors="replace"),
        stderr=result.stderr.decode("utf-8", errors="replace"),
        stdout_truncated=result.stdout_truncated,
        stderr_truncated=result.stderr_truncated,
        duration_seconds=result.duration_seconds,
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def build_default_registry() -> dict[str, Tool]:
    tools: list[Tool] = [ReadFile(), Search(), ApplyPatch(), RunTests(), RunLinter()]
    return {tool.name: tool for tool in tools}


@dataclass
class ToolContext:
    """Per-step tool environment: the workspace tree, run identity, budgets,
    the injected sandbox, and this step's accumulating patch proposal.

    `conn` is the connection tool_calls rows are written on (the worker's
    autocommit connection during handler execution); None only in pure
    unit contexts that don't log rows.
    """

    workspace: WorkspaceTree
    workspace_id: int
    parent_state_hash: str  # the log head at step start: the append's parent
    job_id: UUID
    attempt: int
    # STEP IDENTITY for patch-log idempotency (migration 0007): MUST carry
    # the workflow id and step position, e.g. "wf:<uuid>/step:<pos>", and
    # MUST be the same across attempts of one step — a matching run_ref is
    # the redelivery signal, so attempt-varying values here would break
    # idempotency. Empty string only in non-workspace callers.
    run_ref: str
    conn: Any | None = None
    budgets: ToolBudgets = field(default_factory=ToolBudgets)
    sandbox_runner: SandboxRunner | None = None
    allowlist: CommandAllowlist | None = None
    registry: dict[str, Tool] = field(default_factory=build_default_registry)
    pending_edits: list[PatchEdit] = field(default_factory=list)
    _calls_used: int = field(default=0, init=False, repr=False)
    _output_bytes: int = field(default=0, init=False, repr=False)

    def invoke(self, name: str, raw_args: dict[str, Any]) -> ToolResult:
        """The only door to a tool: validate, budget, run, log."""
        tool = self.registry.get(name)
        if tool is None:
            return self._refused(name, raw_args, "unknown_tool", f"no tool named {name!r}")
        if self._calls_used >= self.budgets.max_calls_per_run:
            return self._refused(
                name,
                raw_args,
                "budget_exhausted",
                f"per-run tool call budget ({self.budgets.max_calls_per_run}) spent",
            )
        if self._output_bytes >= self.budgets.max_output_bytes_per_run:
            return self._refused(
                name,
                raw_args,
                "budget_exhausted",
                f"per-run tool output budget ({self.budgets.max_output_bytes_per_run} bytes) spent",
            )
        try:
            args = tool.input_model.model_validate(raw_args)
        except ValidationError as exc:
            return self._refused(name, raw_args, "invalid_args", _short_validation_error(exc))
        call_index = self._calls_used
        started = time.perf_counter()
        try:
            output = tool.run(args, self)
            data: dict[str, Any] = output.model_dump(mode="json")
            ok = True
            error_code: str | None = None
            message: str | None = None
        except ToolError as exc:
            data = {"message": _row_safe_text(str(exc))}
            ok = False
            error_code = exc.code
            message = _row_safe_text(str(exc))
        except Exception as exc:  # any crash becomes structured internal_error
            detail = _row_safe_text(repr(exc))[:500]
            data = {"message": detail}
            ok = False
            error_code = "internal_error"
            message = detail
        duration = time.perf_counter() - started
        output_bytes = len(json.dumps(data, allow_nan=False, default=str).encode("utf-8"))
        self._calls_used += 1
        self._output_bytes += output_bytes
        self._write_row(
            call_index=call_index,
            tool_name=name,
            args=args if ok else None,
            raw_args=raw_args if not ok else None,
            ok=ok,
            error_code=error_code,
            message=message,
            duration_seconds=duration,
            output_bytes=output_bytes,
        )
        return ToolResult(
            ok=ok,
            data=data,
            error_code=error_code,
            duration_seconds=duration,
            output_bytes=output_bytes,
        )

    def _refused(self, name: str, raw_args: dict[str, Any], code: str, message: str) -> ToolResult:
        """A call refused before execution: still logged, zero output."""
        call_index = self._calls_used
        self._calls_used += 1
        self._write_row(
            call_index=call_index,
            tool_name=name,
            args=None,
            raw_args=raw_args,
            ok=False,
            error_code=code,
            message=_row_safe_text(message),
            duration_seconds=0.0,
            output_bytes=0,
        )
        return ToolResult(
            ok=False,
            data={"message": _row_safe_text(message)},
            error_code=code,
            duration_seconds=0.0,
            output_bytes=0,
        )

    def _write_row(
        self,
        *,
        call_index: int,
        tool_name: str,
        args: BaseModel | None,
        raw_args: dict[str, Any] | None,
        ok: bool,
        error_code: str | None,
        message: str | None,
        duration_seconds: float,
        output_bytes: int,
    ) -> None:
        if self.conn is None:
            return
        try:
            if args is not None:
                dumped: Any = args.model_dump(mode="json")
            else:
                dumped = raw_args if raw_args is not None else {}
            # Arguments are NEVER stored verbatim: apply_patch arguments are
            # megabytes of repository content and can contain repo secrets.
            # The row carries the canonical JSON's digest, its size, and a
            # 200-char preview for triage.
            canonical = _row_safe_json(dumped)
            args_text = _nul_free_json(canonical)
            self.conn.execute(
                _TOOL_CALL_INSERT_SQL,
                {
                    "job_id": self.job_id,
                    "attempt": self.attempt,
                    "call_index": call_index,
                    "tool": tool_name,
                    "args_sha256": sha256_hex(args_text.encode("utf-8")),
                    "args_bytes": len(args_text.encode("utf-8")),
                    "args_preview": _row_safe_text(args_text[:_ARGS_PREVIEW_CHARS]),
                    "ok": ok,
                    "error_code": error_code,
                    "error_message": None
                    if message is None
                    else message[:_ERROR_MESSAGE_MAX_CHARS],
                    "duration_ms": duration_seconds * 1000.0,
                    "output_bytes": output_bytes,
                },
            )
        except Exception:
            # the diagnostic row must never break the tool result; the
            # tool's own effects (tree, pending patch) are untouched
            pass


def _short_validation_error(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "invalid arguments"
    first = errors[0]
    loc = first.get("loc")
    msg = first.get("msg")
    return f"invalid arguments: {loc} {msg}"


def _strip_nul(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\x00", "\ufffd")
    if isinstance(value, list):
        return [_strip_nul(item) for item in value]
    if isinstance(value, dict):
        return {key: _strip_nul(item) for key, item in value.items()}
    return value


def _row_safe_json(payload: Any) -> Any:
    """Sanitize before hashing: jsonb is gone, but NUL never enters a row."""
    clean = _strip_nul(payload)
    if isinstance(clean, dict):
        return clean
    return {"_payload": clean}


def _nul_free_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=True, allow_nan=False, default=str)


def _row_safe_text(text: str) -> str:
    return text.replace("\x00", "\ufffd")


# ---------------------------------------------------------------------------
# The advance-transaction hook (Task 3): the handler registers this via
# after_complete; the append runs inside the same transaction as the job's
# fenced complete — a stale worker's callback raises and everything rolls
# back together.
# ---------------------------------------------------------------------------


def patch_log_append_callback(ctx: ToolContext) -> Callable[[Any], None]:
    """Registered with `after_complete`: appends the step's accumulated
    patch inside the advance transaction.

    Idempotency contract (migration 0007): `ctx.run_ref` is the step's
    identity — the same value on every attempt of one step, carrying the
    workflow id and step position ("wf:<uuid>/step:<pos>"). The append is
    redelivery-safe BECAUSE of that identity: a run_ref match with the same
    parent+blob is `already_applied`, and a match with different content is
    a PatchConflict. Content keys (parent hash + patch bytes) cannot do
    this: state hashes are content hashes, so A->B -> revert -> a new A->B
    from another step would false-positive on a content lookup.
    """

    def _append(conn: Any) -> None:
        if not ctx.pending_edits:
            return  # a read-only step proposes nothing; nothing to append
        from . import patchlog

        new_hash = ctx.workspace.state_hash()
        patch = canonical_patch_json(ctx.parent_state_hash, new_hash, ctx.pending_edits)
        patchlog.append_patch(
            conn,
            workspace_id=ctx.workspace_id,
            parent_state_hash=ctx.parent_state_hash,
            new_state_hash=new_hash,
            patch_bytes=patch,
            run_ref=ctx.run_ref,
        )

    return _append


PATCH_APPEND_CONTRACT = f"patch bytes are canonical JSON; the cap is {PATCH_MAX_BYTES} bytes"
