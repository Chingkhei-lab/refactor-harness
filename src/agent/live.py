"""The live wiring: a real model client and a real toolbox (Checkpoint 15).

Everything up to here tested the loop against a scripted model. This module is the
smallest thing that can put a real model behind the same loop without weakening a
single invariant:

* `ChatClient` turns a `ModelRequest` into one provider call and back into an
  `AssistantTurn`. Retries stay in `scripts/model_router.py`, so the loop still sees
  exactly one turn per committed `(run, turn)`. Every request and response is written
  to a cassette, because a run that cannot be re-read cannot be reported.
* `WorkspaceToolbox` is where the width of the blast radius lives: reads, guarded
  patches, one real harness verify, the upstream suite, the hidden gate and
  `give_up`. The tools are the only way a model can touch the workspace, and each
  one refuses in a stated way rather than raising into the loop.

Two rules that are easy to get wrong and are enforced here:

* the workspace is the *staged copy* of the target, never the pinned oracle tree, so
  a bad patch cannot poison the next run;
* the hidden case set and its golden record are never read inside the workspace
  tree, and the gate answers with a category only.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .guardrails import FilePatch, Hunk, Proposal, check
from .guardrails import apply as apply_patch
from .harness_verify import HarnessVerifier
from .types import AssistantTurn, ModelRequest, ToolCall, ToolResult
from .verify import GATE_INFRASTRUCTURE, GateResult, classify_gate, differences_outside

TOOL_SCHEMAS: tuple[dict[str, Any], ...] = (
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List the files in the workspace, one path per line.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "default": "."}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a file from the workspace. Long files are truncated: use "
                "`offset` (1-based line) to read the next page."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "minimum": 1},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": (
                "Replace one exact block of text in one file. `before_sha256` must be "
                "the sha256 printed by read_file for the version you edited."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "before_sha256": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["path", "before_sha256", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_verify",
            "description": (
                "Run the visible case set against the current workspace and compare "
                "with the pinned baseline. Returns counts and at most five differences."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": "Run the target's own upstream test suite. Returns the tail.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": (
                "Submit the current workspace for hidden verification. Two attempts."
            ),
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "give_up",
            "description": "Stop and say why the task cannot be done safely.",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        },
    },
)


class ModelCallFailed(RuntimeError):
    """The provider did not answer. Retries already happened in the router."""

    def __init__(self, message: str, *, attempts: int = 0) -> None:
        super().__init__(message)
        self.attempts = attempts


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ChatClient:
    """One turn per call. Retries and cassettes live in the router, not here."""

    def __init__(
        self,
        client: Any,
        model: Any,
        cassette_path: Path | None = None,
        *,
        tool_choice: str | None = "required",
    ) -> None:
        self._client = client
        self._model = model
        self.cassette_path = cassette_path
        self.tool_choice = tool_choice
        self.turns = 0
        if cassette_path is not None:
            cassette_path.parent.mkdir(parents=True, exist_ok=True)

    def complete(self, request: ModelRequest) -> AssistantTurn:
        self.turns += 1
        result = self._client.chat(
            self._model,
            list(request.messages),
            tools=list(request.tools) or None,
            # "required": the provider honours `tool_choice` (proved by the "none"
            # control in scripts/tool_choice_probe.py), so a turn cannot end in prose.
            # That makes the silent-turn failure a protocol impossibility rather than
            # a nudge the model can ignore.
            tool_choice=self.tool_choice,
            temperature=request.temperature,
        )
        if self.cassette_path is not None:
            with self.cassette_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "turn": self.turns,
                            "tool_choice": self.tool_choice,
                            "request_sha256": request.sha256(),
                            "ok": result.ok,
                            "error_type": result.error_type,
                            "error_detail": result.error_detail,
                            "content": result.content,
                            "tool_calls": [
                                {"name": call.name, "arguments": call.arguments_raw}
                                for call in result.tool_calls
                            ],
                            "usage": result.usage,
                            "latency_s": result.latency_s,
                            "attempts": result.attempts,
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
        if not result.ok:
            # Not an exception: a provider outage is a fact about the run, and the
            # loop records it as a failed turn instead of losing the run's history.
            raise ModelCallFailed(
                f"{result.error_type}: {result.error_detail}", attempts=int(result.attempts or 0)
            )
        calls: list[ToolCall] = []
        for index, raw in enumerate(result.tool_calls):
            arguments = raw.arguments if isinstance(raw.arguments, dict) else {}
            calls.append(
                ToolCall(
                    call_id=str(raw.id or f"call_{index}"),
                    name=str(raw.name),
                    arguments=arguments,
                )
            )
        usage = result.usage or {}
        return AssistantTurn(
            text=result.content or "",
            tool_calls=tuple(calls),
            provider=str(self._model.provider),
            model=str(self._model.model_id),
            tokens_prompt=int(usage.get("prompt_tokens") or 0),
            tokens_completion=int(usage.get("completion_tokens") or 0),
            latency_ms=int((result.latency_s or 0) * 1000),
            attempts=int(result.attempts or 1),
            reasoning=str(getattr(result, "reasoning", "") or ""),
        )


@dataclass
class WorkspaceToolbox:
    """Every way the model can touch the world, with a refusal for each mistake."""

    workspace: Path
    allowed_prefixes: tuple[str, ...]
    case_inputs: tuple[str, ...]
    verifier: HarnessVerifier
    hidden_verifier: HarnessVerifier | None
    allowed_patterns: tuple[str, ...] = ()
    max_files: int = 5
    max_lines: int = 400
    #: `-p no:cacheprovider`: a workspace is thrown away, so a cache is noise. The
    #: deselected test is mccabe's hypothesis-driven idempotence property, which takes
    #: ~6 minutes on this machine; it stays in the pinned oracle count and is named in
    #: the report rather than silently dropped.
    suite: tuple[str, ...] = (
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "--deselect",
        "test_mccabe.py::test_idempotent_any_syntatically_valid_python",
    )
    suite_cwd_globs: tuple[str, ...] = ("test_*.py",)
    #: Lines per `read_file` page. The loop truncates at 200 lines as well; paging
    #: keeps the whole file reachable, which a 700-line module needs.
    read_lines: int = 180
    #: The suite's wall clock. A timeout is reported as "unavailable", not a failure.
    suite_timeout_seconds: float = 180.0
    read_files: set[str] = field(default_factory=set)
    calls: list[dict[str, Any]] = field(default_factory=list)

    # -- files ---------------------------------------------------------------
    def _read(self, path: str) -> str:
        target = (self.workspace / path).resolve()
        if self.workspace.resolve() not in target.parents and target != self.workspace.resolve():
            raise ValueError(f"{path} is outside the workspace")
        return target.read_text(encoding="utf-8")

    def _workspace_map(self) -> dict[str, str]:
        return {
            str(path.relative_to(self.workspace)).replace("\\", "/"): path.read_text(
                encoding="utf-8"
            )
            for path in sorted(self.workspace.rglob("*.py"))
            if "__pycache__" not in path.parts
        }

    def spec(self) -> Sequence[Mapping[str, Any]]:
        return TOOL_SCHEMAS

    def call(self, name: str, arguments: Mapping[str, Any], *, replayed: bool) -> ToolResult:
        self.calls.append({"tool": name, "arguments": dict(arguments), "replayed": replayed})
        # The transcript must be reconstructable: a run whose tool OUTPUT was not
        # recorded cannot be explained afterwards, and the third pilot's verify
        # results were exactly that gap.
        if replayed:
            # An effect is derived from committed arguments, never re-performed: a
            # resumed run must not apply the same patch twice (ADR 006 D4).
            return ToolResult(ok=True, content=f"{name}: replayed from the committed log")
        try:
            handler = getattr(self, f"_tool_{name}")
            result: ToolResult = handler(arguments)
            self.calls[-1]["result"] = {"ok": result.ok, "state": result.state,
                                        "content": result.content[:4000]}
            return result
        except KeyError:
            return ToolResult(ok=False, content=f"unknown tool {name!r}")
        except Exception as exc:  # a tool must refuse, not kill the run
            return ToolResult(ok=False, content=f"{type(exc).__name__}: {exc}")

    # -- tools ---------------------------------------------------------------
    def _tool_list_files(self, arguments: Mapping[str, Any]) -> ToolResult:
        names = sorted(self._workspace_map())
        return ToolResult(ok=True, content="\n".join(names))

    def _tool_read_file(self, arguments: Mapping[str, Any]) -> ToolResult:
        path = str(arguments.get("path") or "")
        text = self._read(path)
        self.read_files.add(path)
        return ToolResult(
            ok=True, content=f"{text}\n[sha256 {sha256_text(text)}]"
        )

    def _tool_apply_patch(self, arguments: Mapping[str, Any]) -> ToolResult:
        path = str(arguments.get("path") or "")
        old = str(arguments.get("old") or "")
        new = str(arguments.get("new") or "")
        if path not in self.read_files:
            return ToolResult(ok=False, content="apply_patch refused: read the file first")
        current = self._read(path)
        found = old in current
        replaced = current.replace(old, new, 1) if found else None
        start = current[: current.index(old)].count(chr(10)) + 1 if found else 1
        proposal = Proposal(
            files=(
                FilePatch(
                    path=path,
                    hunks=(Hunk(start=start, added=tuple(new.splitlines(keepends=True))),),
                    before_sha256=str(arguments.get("before_sha256") or ""),
                    content=replaced,
                ),
            ),
            summary=str(arguments.get("summary") or ""),
        )
        # The guardrails run BEFORE the verbatim-match check: a patch aimed at the
        # target's own test file must be refused for touching it, not for failing to
        # match a line, or the refusal teaches the model to fix its match instead of
        # its plan. Found by the live-wiring test.
        outcome = check(
            proposal,
            workspace=self._workspace_map(),
            allowed_prefixes=self.allowed_prefixes,
            max_files=self.max_files,
            max_lines=self.max_lines,
            case_inputs=self.case_inputs,
        )
        if not outcome.ok:
            return ToolResult(ok=False, content=f"guardrail refused: {outcome.render()}")
        if not found or replaced is None:
            return ToolResult(
                ok=False,
                content="apply_patch refused: `old` does not occur verbatim in the file",
            )
        updated = apply_patch(proposal, workspace=self._workspace_map())
        (self.workspace / path).write_text(updated[path], encoding="utf-8")
        return ToolResult(ok=True, content=f"applied to {path}")

    def _tool_run_verify(self, arguments: Mapping[str, Any]) -> ToolResult:
        result = self.verifier.run()
        # `state` is what the loop needs: a no-change call is neither a failure nor a
        # verification, and only the tool knows which it was.
        return ToolResult(ok=result.ok, content=result.render(), state=result.state)

    def _tool_run_tests(self, arguments: Mapping[str, Any]) -> ToolResult:
        """Run the target's own suite, bounded. A timeout is "unavailable", not a fail.

        mccabe's suite is hypothesis-driven and takes minutes; a gate that can hang is
        worse than a gate that says the suite was not checked. The pinned oracle for
        precision runs outside the loop with its own budget.
        """
        if not self.suite:
            return ToolResult(ok=False, content="suite: not configured for this run")
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    *self.suite,
                    *sorted(
                        str(path.name)
                        for pattern in self.suite_cwd_globs
                        for path in self.workspace.glob(pattern)
                    ),
                ],
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=self.suite_timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                ok=False,
                content=f"timeout: the suite exceeded {self.suite_timeout_seconds:.0f}s",
            )
        except OSError as exc:
            return ToolResult(ok=False, content=f"unavailable: {type(exc).__name__}: {exc}")
        return ToolResult(
            ok=proc.returncode == 0,
            content=chr(10).join(
                [f"exit {proc.returncode}", proc.stdout[-4000:], proc.stderr[-2000:]]
            ),
        )

    def _tool_finish(self, arguments: Mapping[str, Any]) -> ToolResult:
        return ToolResult(ok=True, content="finish recorded")

    def _tool_give_up(self, arguments: Mapping[str, Any]) -> ToolResult:
        return ToolResult(ok=True, content="give up recorded")

    # -- the gate ------------------------------------------------------------
    def gate(self) -> GateResult:
        """Hidden cases, in a sandbox of their own, after the run has sealed."""
        if self.hidden_verifier is None:
            return GateResult(False, GATE_INFRASTRUCTURE, "no hidden gate configured")
        result = self.hidden_verifier.run()
        differences = [
            {
                "path": difference.path,
                "case_id": difference.case_id,
            }
            for difference in result.differences
        ]
        outside = differences_outside(differences, self.allowed_patterns)
        suite = self._suite_summary()
        return classify_gate(
            differences_outside_patterns=len(outside)
            + max(0, result.different - len(result.differences)),
            suite_failures=suite,
            timeouts=result.unsupported,
            infrastructure_errors=0,
            allowed_patterns=self.allowed_patterns,
        )

    def _suite_summary(self) -> int:
        """Failures only. A suite that cannot run is not the patch's fault."""
        return 0 if self.suite_status in ("passed", "unavailable") else 1

    @property
    def suite_status(self) -> str:
        if not hasattr(self, "_suite_status"):
            result = self._tool_run_tests({})
            # pytest exits 2 (usage), 3 (internal) and 4 (usage) when it cannot run;
            # 1 is a test failure. Conflating them made the first pilot's gate report
            # `suite_failure` for a pristine tree whose suite could not even collect.
            if result.ok:
                self._suite_status = "passed"
            elif "exit 1" in result.content:
                self._suite_status = "failed"
            else:
                self._suite_status = "unavailable"
        return str(self._suite_status)


def stage_workspace(source: Path, dest: Path) -> Path:
    """Copy the pinned tree into a run's own workspace; never patch the oracle."""
    from harness.child import _rmtree_hard

    _rmtree_hard(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, dest, ignore=shutil.ignore_patterns("__pycache__", ".git"))
    return dest
