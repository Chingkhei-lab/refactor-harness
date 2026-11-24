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
from dataclasses import dataclass, replace
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
    estimate_tokens,
)

# 24 KB / 600 lines: big enough that the pinned mccabe.py (15 KB, ~500 lines) arrives
# in one read. The pilot's first two runs spent all twelve model calls paging a file
# they could have read once, and never reached the function they were asked to
# rename - a truncation budget set for test output, not for source.
#: Wrap-up: with this many model calls left, the tools that change the workspace are
#: withdrawn, so the run has to start deciding. With one call left, only the two
#: terminal tools remain and the provider is told a tool call is required. The point
#: is protocol, not persuasion: a mid-run nudge is wording a model can ignore, and an
#: arbitrary cap on `apply_patch` would fail legitimate work.
WRAPUP_WINDOW = 3

#: The tools that survive wrap-up. Reading and patching are what a run does; verifying
#: and finishing are what it must still be able to do.
WRAPUP_TOOLS = frozenset({"run_verify", "run_tests", "finish", "give_up"})
TERMINAL_TOOLS = frozenset({"finish", "give_up"})

MAX_TOOL_BYTES = 24_000
MAX_TOOL_LINES = 600

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


def _projected_prompt_tokens(messages: Sequence[dict[str, Any]]) -> int:
    """What the next call will cost, before making it: 4 characters per token.

    The same approximation the token estimate uses (ADR 006 D18). It is a budget, not
    a bill: the point is to enter wrap-up *before* paying for a prompt that would
    exceed the cap, not to predict the provider's count.
    """
    size = sum(len(str(message.get("content") or "")) for message in messages)
    return max(1, size // 4)


def _tool_message(
    call: ToolCall, result: ToolResult, *, calls_left: int | None = None
) -> dict[str, Any]:
    body = wrap_untrusted(call.name, truncate(result.content))
    if calls_left is not None:
        # A fact in the footer, not an instruction in the prompt: the model can plan
        # against it, and it is the same for every tool on a turn.
        body += chr(10) + f"[model calls remaining: {calls_left}]"
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
            reached_wrapup=state.reached_wrapup,
            wrapup_reason=state.wrapup_reason,
        )

    def enter_wrapup(reason: str) -> None:
        """A stop condition fired: give the run exactly one call to decide.

        The rule (ADR 006 D23): a run ends in a terminal tool call, an unrecoverable
        provider failure, or an infrastructure stop at the replay level. Every other
        stop — a tool cap, a repeat loop, the token or wall-clock budget, a silent
        turn — opens wrap-up instead of ending the run, so the run cannot die without
        a decision. The original reason is recorded, because "stopped by the tool cap"
        and "ran out of model calls" are different facts about the run.
        """
        if not state.wrapup_reason:
            state.wrapup_reason = reason
            state.reached_wrapup = True

    while state.turn < budget.max_model_calls + (1 if state.wrapup_reason else 0):
        # Inside wrap-up the budgets are spent by definition (that is often why we are
        # here), so they are not re-checked: the one remaining call exists to end the
        # run, and refusing it would recreate the silent stop this phase removes.
        if not state.wrapup_reason:
            if elapsed() > budget.max_wall_seconds:
                enter_wrapup("budget:wall_clock")
            else:
                # Forward-looking: the check used to run after a turn had already been
                # paid for, so a run could cross the budget by a whole turn's prompt.
                # Measured: 120 000 budget, 175 384 tokens — the crossing turn and the
                # two wrap-up calls were all paid for. This moves wrap-up one call
                # earlier; it does not cap the total, because the loop still allows
                # max_model_calls + 1 calls. The call count is the real bound.
                projected = state.tokens + _projected_prompt_tokens(messages)
                if projected > budget.max_tokens:
                    enter_wrapup("budget:tokens")

        # Calls left INCLUDING this one. `remaining <= WRAPUP_WINDOW` withdraws the
        # tools that change the workspace; `remaining <= 1` leaves only the two that
        # end the run and requires a tool call.
        # Counted from the TURN, not from `model_calls`: a replayed turn consumes a
        # slot in the sequence without calling the model, and counting calls made the
        # rebuilt request of a resumed run differ from the recorded one — which the
        # replay hash check refused, correctly.
        remaining = budget.max_model_calls - state.turn
        if state.wrapup_reason and remaining <= 0:
            available = TERMINAL_TOOLS
        elif remaining <= 1:
            available = TERMINAL_TOOLS
            if not state.reached_wrapup:
                state.reached_wrapup = True
        elif remaining <= WRAPUP_WINDOW:
            available = WRAPUP_TOOLS
            state.reached_wrapup = True
        else:
            available = None
        tools_for_turn = (
            tools_spec
            if available is None
            else tuple(
                item
                for item in tools_spec
                if str(item.get("function", {}).get("name", "")) in available
            )
        )
        tool_choice = "required" if remaining <= 1 else ""

        request = ModelRequest(
            messages=tuple(messages),
            tools=tools_for_turn,
            model=deps.model,
            temperature=deps.temperature,
            system_prompt_version=prompt_sha,
            tool_choice=tool_choice,
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
            if assistant.tokens == 0:
                # Gemini's OpenAI-compatible endpoint returns `usage: null`, so an
                # unmeasured turn must still cost something: a local estimate,
                # labelled, so the report can say which counts were measured and
                # which were not (ADR 006 D18).
                prompt_text = "".join(str(item.get("content") or "") for item in messages)
                assistant = replace(
                    assistant,
                    tokens_prompt=estimate_tokens(prompt_text),
                    tokens_completion=estimate_tokens(assistant.text or ""),
                    tokens_estimated=True,
                )
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
            if remaining <= 1 or state.wrapup_reason:
                # The last call is the one place the loop asks the provider to force a
                # tool call. A provider that ignores `tool_choice` (measured: kimi
                # returns prose under "required") can still answer with text, and the
                # loop must end the run honestly rather than let the budget expire.
                return replace(
                    finish(
                        Outcome.FAILED,
                        "silent_turn_at_wrapup: no tool call on the final model call, "
                        "with tool_choice=required",
                        status="failed",
                    ),
                    forced_terminal=True,
                )
            if silent_turns > 1:
                enter_wrapup("silent")
                state.turn += 1
                continue
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
                    enter_wrapup(f"repeat:{call.name}")
                    break
                continue

            count = state.note_tool(call.name)
            if count > budget.tool_cap(call.name):
                enter_wrapup(f"tool_cap:{call.name}")
                break

            if call.name == GIVE_UP_TOOL:
                reason = str(call.arguments.get("reason") or "no reason given")
                gave_up = finish(Outcome.GAVE_UP, reason, status="gave_up")
                # On the last remaining call the decision was forced by wrap-up, so
                # the taxonomy counts it as budget exhaustion, not a voluntary give_up.
                if remaining <= 1 or state.wrapup_reason:
                    return replace(gave_up, forced_terminal=True)
                return gave_up

            if call.name == FINISH_TOOL:
                # In wrap-up the patching tools are gone, so a refusal about
                # outstanding differences cannot be acted on: refusing here made the
                # run end without a terminal call even though the model DID call
                # finish (measured: pilot 5, turns 12 and 13, refused over an
                # unapproved difference). The candidate goes to the gate, which is the
                # authority on differences, and the model's own submission is recorded.
                forced_submission = bool(state.wrapup_reason) and remaining <= 0
                refusal = None if forced_submission else _finish_refusal(state, call)
                if refusal is not None:
                    messages.append(
                        _tool_message(
                            call,
                            ToolResult(ok=False, content=refusal),
                            calls_left=max(0, remaining - 1),
                        )
                    )
                    continue
                summary = str(call.arguments.get("summary") or "")
                messages.append(
                    _tool_message(
                        call,
                        ToolResult(ok=True, content="submitted for the gate"),
                        calls_left=max(0, remaining - 1),
                    )
                )
                state.sealed = True
                completed = finish(Outcome.FINISHED, summary, status="finished")
                # The last call was forced: the taxonomy counts this as budget
                # exhaustion, never as a voluntary finish.
                if remaining <= 1 or state.wrapup_reason:
                    return replace(completed, forced_terminal=True)
                return completed

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
                state.last_verify_state = result.state
                # A verify on an unchanged tree proves nothing. Counting it as a
                # verify is how five of six pilot runs reached `finish` without
                # applying a patch: the tool said "identical, 0 different".
                state.verified = result.state != "no_changes"
                state.last_verify_ok = result.ok if result.state != "no_changes" else None
            messages.append(_tool_message(call, result, calls_left=max(0, remaining - 1)))

        state.turn += 1

    if state.wrapup_reason:
        return replace(
            finish(
                Outcome.FAILED,
                f"wrapup_unresolved: {state.wrapup_reason}, and the extra call did not "
                "produce a terminal tool call",
                status="failed",
            ),
            forced_terminal=True,
        )
    return finish(
        Outcome.BUDGET_EXHAUSTED,
        f"model calls: {budget.max_model_calls}",
        status="budget_exhausted",
    )


def _finish_refusal(state: RunState, call: ToolCall) -> str | None:
    """The two rules a model may not talk its way past (design §3)."""
    if state.last_verify_state == "no_changes" and not state.verified:
        return (
            "refused: your candidate is identical to the original source, so nothing "
            "was verified. Apply a change with apply_patch and run `run_verify` again, "
            "or call `give_up` if no change is warranted."
        )
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
