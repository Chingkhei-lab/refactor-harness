"""The agent loop (ADR 006).

The loop is the only thing that can spend money and the only thing that can write
into the candidate tree, so it holds the invariants that cannot be trusted to a
model:

  * a committed `(run, turn)` is never generated twice (D2);
  * a replayed turn must hash to the same request (D3);
  * tool output is truncated and wrapped as untrusted data (D7);
  * `apply_patch` needs a prior read of that file, `finish` needs a prior
    `run_verify`, and identical repeated calls are refused (D5, D6, design §3).

Everything else — which file to read, what to patch, whether to verify again — is
the model's business. That division is the point: the loop enforces what a wrong
answer could hide, and leaves style to the thing being measured.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .prompts import SYSTEM_PROMPT, system_prompt_sha256
from .store import CallStore, DivergenceError
from .types import (
    FINISH_TOOL,
    GIVE_UP_TOOL,
    PATCH_TOOL,
    READ_TOOLS,
    VERIFY_TOOL,
    AssistantTurn,
    Budget,
    ModelRequest,
    Outcome,
    RunResult,
    RunState,
    ToolCall,
    ToolResult,
    canonical_json,
)

MAX_TOOL_BYTES = 8_000
MAX_TOOL_LINES = 200

TRUNCATION_MARKER = "…[truncated: {bytes} bytes, {lines} lines]"

NUDGE_AFTER_SILENT_TURN = (
    "You must end with a tool call. Call `finish` (with the summary) or "
    "`give_up` (with the reason). A plain text answer is not a submission."
)


class ModelClient(Protocol):
    """A model that answers one request. Retries live here, not in the loop."""

    def complete(self, request: ModelRequest) -> AssistantTurn: ...


class Toolbox(Protocol):
    def spec(self) -> Sequence[Mapping[str, Any]]: ...

    def call(
        self, name: str, arguments: Mapping[str, Any], *, replayed: bool
    ) -> ToolResult: ...


@dataclass
class AgentDeps:
    """Bundled so the loop signature stays readable and the tests stay short."""

    client: ModelClient
    toolbox: Toolbox
    store: CallStore
    provider: str
    model: str
    temperature: float = 0.0
    budget: Budget = Budget()
    clock: Callable[[], float] = time.monotonic
    system_prompt: str = SYSTEM_PROMPT


def truncate(text: str, *, max_bytes: int = MAX_TOOL_BYTES, max_lines: int = MAX_TOOL_LINES) -> str:
    """Cap a tool result, keeping head and tail and saying exactly what was cut.

    Head and tail, not head only: the useful part of a test output is the summary
    at the end, and a head-only truncation is how a model concludes "no failures"
    from a prefix that never reached them.
    """
    encoded = text.encode("utf-8")
    lines = text.splitlines()
    if len(encoded) <= max_bytes and len(lines) <= max_lines:
        return text
    head_bytes = max_bytes // 2
    tail_bytes = max_bytes - head_bytes
    head = encoded[:head_bytes].decode("utf-8", errors="ignore")
    tail = encoded[-tail_bytes:].decode("utf-8", errors="ignore")
    marker = TRUNCATION_MARKER.format(
        bytes=max(0, len(encoded) - len(head.encode()) - len(tail.encode())),
        lines=max(0, len(lines) - len(head.splitlines()) - len(tail.splitlines())),
    )
    return f"{head}\n{marker}\n{tail}"


def wrap_untrusted(name: str, content: str) -> str:
    """Every tool result is data. The wrapper is the harness's, not the model's."""
    return f'<tool_output tool="{name}" untrusted="true">\n{content}\n</tool_output>'


def _assistant_message(assistant: AssistantTurn) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": assistant.text}
    if assistant.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.call_id,
                "type": "function",
                "function": {"name": call.name, "arguments": canonical_json(call.arguments)},
            }
            for call in assistant.tool_calls
        ]
        # Provider extras (Gemini's thought_signature) are replayed verbatim; a
        # reconstructed message is a 400 on the next turn.
        for extra in assistant.raw.get("extra_content") or ():
            message.setdefault("extra_content", []).append(extra)
    return message


def _tool_message(call: ToolCall, result: ToolResult) -> dict[str, Any]:
    body = wrap_untrusted(call.name, truncate(result.content))
    if not result.ok:
        body = 'error="true"\n' + body if False else body
    return {"role": "tool", "tool_call_id": call.call_id, "content": body}


def run_agent(
    *,
    run_id: str,
    task: str,
    deps: AgentDeps,
    task_id: str = "",
) -> RunResult:
    budget = deps.budget
    prompt_sha = system_prompt_sha256()
    deps.store.start_run(
        run_id=run_id,
        task_id=task_id or run_id,
        provider=deps.provider,
        model=deps.model,
        prompt_sha256=prompt_sha,
        temperature=deps.temperature,
        budget=budget,
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": deps.system_prompt},
        {"role": "user", "content": task},
    ]
    state = RunState(run_id=run_id)
    committed = {turn.turn: turn for turn in deps.store.committed(run_id)}
    started = deps.clock()
    tools_spec = tuple(dict(item) for item in deps.toolbox.spec())
    silent_turns = 0

    def elapsed() -> float:
        return deps.clock() - started

    def finish(
        outcome: Outcome,
        reason: str,
        status: str = "finished",
    ) -> RunResult:
        deps.store.finish(
            run_id=run_id,
            task_id=task_id or run_id,
            provider=deps.provider,
            model=deps.model,
            prompt_sha256=prompt_sha,
            temperature=deps.temperature,
            budget=budget,
            status=status,
            outcome=outcome,
            reason=reason,
        )
        return RunResult(
            outcome=outcome,
            reason=reason,
            state=state,
            wall_seconds=elapsed(),
            messages=tuple(messages),
        )

    while state.turn < budget.max_model_calls:
        if elapsed() > budget.max_wall_seconds:
            return finish(
                Outcome.BUDGET_EXHAUSTED,
                f"wall clock: {elapsed():.0f}s > {budget.max_wall_seconds:.0f}s",
                status="budget_exhausted",
            )
        if state.tokens > budget.max_tokens:
            return finish(
                Outcome.BUDGET_EXHAUSTED,
                f"tokens: {state.tokens} > {budget.max_tokens}",
                status="budget_exhausted",
            )

        request = ModelRequest(
            messages=tuple(messages),
            tools=tools_spec,
            model=deps.model,
            temperature=deps.temperature,
            system_prompt_version=prompt_sha,
        )
        request_hash = request.sha256()

        replay = committed.get(state.turn)
        if replay is not None:
            if replay.request_sha256 != request_hash:
                raise DivergenceError(
                    f"turn {state.turn} was committed with request "
                    f"{replay.request_sha256[:12]}, rebuilt as {request_hash[:12]}: "
                    "prompt, tools or history changed since it was recorded"
                )
            if replay.status != "ok":
                return finish(
                    Outcome.FAILED,
                    f"turn {state.turn} is recorded as {replay.status}: {replay.error}",
                    status="failed",
                )
            assistant = replay.assistant
            state.replayed += 1
        else:
            assistant = deps.client.complete(request)
            state.model_calls += 1
            deps.store.append(
                run_id=run_id,
                turn=state.turn,
                request_sha256=request_hash,
                assistant=assistant,
                provider=assistant.provider or deps.provider,
                model=assistant.model or deps.model,
            )
        state.tokens += assistant.tokens
        messages.append(_assistant_message(assistant))

        if not assistant.tool_calls:
            silent_turns += 1
            if silent_turns > 1:
                return finish(
                    Outcome.BUDGET_EXHAUSTED,
                    "model produced text without a tool call twice",
                    status="budget_exhausted",
                )
            messages.append({"role": "user", "content": NUDGE_AFTER_SILENT_TURN})
            state.turn += 1
            continue

        for call in assistant.tool_calls:
            signature = call.signature()
            if state.note_signature(signature, budget.repeat_limit):
                messages.append(
                    _tool_message(
                        call,
                        ToolResult(
                            ok=False,
                            content=(
                                "refused: this is the same call with the same "
                                "arguments as before. Read the previous result "
                                "and take a different step."
                            ),
                        ),
                    )
                )
                if state.repeats > budget.repeat_limit:
                    return finish(
                        Outcome.BUDGET_EXHAUSTED,
                        f"repeat: {call.name} called with identical arguments "
                        f"{state.repeats} times",
                        status="budget_exhausted",
                    )
                continue

            count = state.note_tool(call.name)
            if count > budget.tool_cap(call.name):
                return finish(
                    Outcome.BUDGET_EXHAUSTED,
                    f"tool cap: {call.name} called {count} times, "
                    f"cap {budget.tool_cap(call.name)}",
                    status="budget_exhausted",
                )

            if call.name == GIVE_UP_TOOL:
                reason = str(call.arguments.get("reason") or "no reason given")
                return finish(Outcome.GAVE_UP, reason, status="gave_up")

            if call.name == FINISH_TOOL:
                refusal = _finish_refusal(state, call)
                if refusal is not None:
                    messages.append(_tool_message(call, ToolResult(ok=False, content=refusal)))
                    continue
                summary = str(call.arguments.get("summary") or "")
                messages.append(
                    _tool_message(call, ToolResult(ok=True, content="submitted for the gate"))
                )
                state.sealed = True
                return finish(Outcome.FINISHED, summary, status="finished")

            if call.name == PATCH_TOOL:
                path = str(call.arguments.get("path") or "")
                if path not in state.read_paths:
                    messages.append(
                        _tool_message(
                            call,
                            ToolResult(
                                ok=False,
                                content=f"refused: read {path!r} before patching it",
                            ),
                        )
                    )
                    continue

            result = deps.toolbox.call(call.name, call.arguments, replayed=replay is not None)
            if call.name in READ_TOOLS and result.ok:
                state.read_paths.add(str(call.arguments.get("path") or ""))
            if call.name == VERIFY_TOOL:
                state.verified = True
                state.last_verify_ok = result.ok
            messages.append(_tool_message(call, result))

        state.turn += 1

    return finish(
        Outcome.BUDGET_EXHAUSTED,
        f"model calls: {budget.max_model_calls}",
        status="budget_exhausted",
    )


def _finish_refusal(state: RunState, call: ToolCall) -> str | None:
    """The two rules a model may not talk its way past (design §3)."""
    if not state.verified:
        return "refused: run `run_verify` at least once before finishing"
    if state.last_verify_ok is False:
        return (
            "refused: the last `run_verify` reported differing cases. Explain them "
            "in the report or revise the patch; a difference is not yours to approve."
        )
    if not str(call.arguments.get("summary") or "").strip():
        return "refused: `finish` needs a summary of what changed and what was verified"
    return None
