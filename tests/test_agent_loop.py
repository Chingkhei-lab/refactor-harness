"""The agent loop against a scripted model (Checkpoint 13).

No network, no real client: a fake returns canned turns, so each test asserts one
invariant of the loop rather than a model's behaviour. The invariants are the ones
ADR 006 says cannot be left to judgement — read-before-patch, verify-before-finish,
budget accounting, truncation, untrusted wrapping and repeat refusal.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import uuid4

import pytest

from agent import (
    AgentDeps,
    AssistantTurn,
    Budget,
    CallStore,
    Outcome,
    RunResult,
    ToolCall,
    ToolResult,
    run_agent,
    truncate,
    wrap_untrusted,
)
from agent.loop import ModelClient
from agent.prompts import system_prompt_sha256
from agent.types import ModelRequest


def turn(*calls: ToolCall, text: str = "", tokens: int = 10) -> AssistantTurn:
    return AssistantTurn(
        text=text,
        tool_calls=calls,
        provider="fake",
        model="fake-1",
        tokens_prompt=tokens // 2,
        tokens_completion=tokens - tokens // 2,
        latency_ms=1,
        attempts=1,
    )


def call(name: str, **arguments: Any) -> ToolCall:
    return ToolCall(call_id=f"call-{uuid4().hex[:8]}", name=name, arguments=arguments)


class ScriptedClient:
    """Returns scripted turns in order and counts how often it was asked."""

    def __init__(self, turns: Sequence[AssistantTurn]) -> None:
        self._turns = list(turns)
        self.requests: list[ModelRequest] = []

    @property
    def calls(self) -> int:
        return len(self.requests)

    def complete(self, request: ModelRequest) -> AssistantTurn:
        self.requests.append(request)
        if not self._turns:
            raise AssertionError("model asked for more turns than the script has")
        return self._turns.pop(0)


class FakeToolbox:
    """Records tool calls and answers from a name -> result table."""

    def __init__(
        self,
        results: Mapping[str, ToolResult] | None = None,
        *,
        verify_ok: bool = True,
    ) -> None:
        self._results = dict(results or {})
        self._verify_ok = verify_ok
        self.calls: list[tuple[str, Mapping[str, Any], bool]] = []
        self.writes_derived: list[str] = []

    def spec(self) -> Sequence[Mapping[str, Any]]:
        return [
            {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}
            for name in ("read_file", "apply_patch", "run_verify", "finish", "give_up")
        ]

    def call(self, name: str, arguments: Mapping[str, Any], *, replayed: bool) -> ToolResult:
        self.calls.append((name, arguments, replayed))
        if replayed:
            self.writes_derived.append(name)
        if name == "run_verify":
            return ToolResult(ok=self._verify_ok, content="identical=80 different=0")
        return self._results.get(name, ToolResult(ok=True, content=f"{name} ok"))

    @property
    def names(self) -> list[str]:
        return [name for name, _, _ in self.calls]


def deps(
    clean_db: Any, client: ModelClient, toolbox: FakeToolbox, budget: Budget | None = None
) -> AgentDeps:
    return AgentDeps(
        client=client,
        toolbox=toolbox,
        store=CallStore(clean_db.database_url),
        provider="fake",
        model="fake-1",
        temperature=0.0,
        budget=budget or Budget(),
    )


def happy_script() -> list[AssistantTurn]:
    return [
        turn(call("read_file", path="pycodestyle.py")),
        turn(call("apply_patch", path="pycodestyle.py", patch="--- a\n+++ b\n")),
        turn(call("run_verify")),
        turn(call("finish", summary="renamed a local; cases identical")),
    ]


def test_the_loop_walks_plan_act_verify_finish(clean_db: Any) -> None:
    run_id = str(uuid4())
    client = ScriptedClient(happy_script())
    toolbox = FakeToolbox()
    result = run_agent(run_id=run_id, task="rename a local", deps=deps(clean_db, client, toolbox))

    assert result.outcome is Outcome.FINISHED
    assert result.reason == "renamed a local; cases identical"
    assert toolbox.names == ["read_file", "apply_patch", "run_verify"]
    assert client.calls == 4
    assert result.state.model_calls == 4
    assert result.state.replayed == 0

    stored = CallStore(clean_db.database_url).committed(run_id)
    assert [item.turn for item in stored] == [0, 1, 2, 3]
    assert json.loads(json.dumps(stored[0].assistant.to_json()))["tool_calls"][0]["name"] == (
        "read_file"
    )


def test_patch_before_read_is_refused_and_the_run_continues(clean_db: Any) -> None:
    """The refusal is a tool result the model can act on, not an exception."""
    run_id = str(uuid4())
    client = ScriptedClient(
        [
            turn(call("apply_patch", path="pycodestyle.py", patch="x")),
            turn(call("read_file", path="pycodestyle.py")),
            turn(call("apply_patch", path="pycodestyle.py", patch="x")),
            turn(call("run_verify")),
            turn(call("finish", summary="ok")),
        ]
    )
    toolbox = FakeToolbox()
    result = run_agent(run_id=run_id, task="patch", deps=deps(clean_db, client, toolbox))

    assert result.outcome is Outcome.FINISHED
    assert toolbox.names == ["read_file", "apply_patch", "run_verify"]
    refusal = [
        message
        for message in result.messages
        if message.get("role") == "tool" and "read 'pycodestyle.py'" in message["content"]
    ]
    assert len(refusal) == 1, "the first patch must be refused with a readable reason"


def test_finish_before_verify_is_refused(clean_db: Any) -> None:
    run_id = str(uuid4())
    client = ScriptedClient(
        [
            turn(call("finish", summary="trust me")),
            turn(call("run_verify")),
            turn(call("finish", summary="verified")),
        ]
    )
    result = run_agent(run_id=run_id, task="patch", deps=deps(clean_db, client, FakeToolbox()))

    assert result.outcome is Outcome.FINISHED
    assert result.reason == "verified"
    assert any(
        message.get("role") == "tool" and "run `run_verify` at least once" in message["content"]
        for message in result.messages
    )


def test_finish_is_refused_while_the_last_verify_reported_differences(clean_db: Any) -> None:
    """A model may not approve its own difference; the report says so."""
    run_id = str(uuid4())
    client = ScriptedClient(
        [
            turn(call("run_verify")),
            turn(call("finish", summary="one difference, but it is fine")),
            turn(call("give_up", reason="the difference is real")),
        ]
    )
    toolbox = FakeToolbox(verify_ok=False)
    result = run_agent(
        run_id=run_id, task="patch", deps=deps(clean_db, client, toolbox, Budget(max_model_calls=4))
    )

    assert result.outcome is Outcome.GAVE_UP
    assert any(
        "a difference is not yours to approve" in message.get("content", "")
        for message in result.messages
        if message.get("role") == "tool"
    )


def test_give_up_is_an_outcome_with_a_reason(clean_db: Any) -> None:
    run_id = str(uuid4())
    client = ScriptedClient([turn(call("give_up", reason="cannot be done without a test change"))])
    result = run_agent(run_id=run_id, task="trap", deps=deps(clean_db, client, FakeToolbox()))

    assert result.outcome is Outcome.GAVE_UP
    assert "without a test change" in result.reason
    assert CallStore(clean_db.database_url).run(run_id)["status"] == "gave_up"


def test_model_call_budget_is_counted_and_reported(clean_db: Any) -> None:
    run_id = str(uuid4())
    client = ScriptedClient([turn(call("read_file", path=f"f{n}.py")) for n in range(5)])
    toolbox = FakeToolbox()
    result = run_agent(
        run_id=run_id,
        task="loop",
        deps=deps(clean_db, client, toolbox, Budget(max_model_calls=3)),
    )

    assert result.outcome is Outcome.BUDGET_EXHAUSTED
    assert result.reason == "model calls: 3"
    assert client.calls == 3


def test_tool_cap_counts_actual_calls_and_has_slack(clean_db: Any) -> None:
    """A cap of expected+slack, not the documented 4 that failed every baked-off model."""
    run_id = str(uuid4())
    client = ScriptedClient(
        [turn(call("read_file", path=f"f{n}.py")) for n in range(6)]
        + [turn(call("give_up", reason="hit the read cap"))]
    )
    budget = Budget.with_tool_slack({"read_file": 1}, max_model_calls=6)
    assert budget.tool_cap("read_file") == 3  # 1 + max(2, ceil(0.5))

    # The cap no longer ends the run: it opens wrap-up, which prompts for one more
    # call whose only options are the terminal tools (ADR 006 D23).
    result = run_agent(
        run_id=run_id, task="reads", deps=deps(clean_db, client, FakeToolbox(), budget)
    )
    assert result.outcome is Outcome.GAVE_UP
    assert result.wrapup_reason == "tool_cap:read_file"
    assert result.forced_terminal


def test_repeated_identical_calls_are_refused_then_end_the_run(clean_db: Any) -> None:
    run_id = str(uuid4())
    same = call("read_file", path="a.py")
    client = ScriptedClient(
        [turn(same), turn(same), turn(same), turn(same), turn(call("give_up", reason="loop"))]
    )
    toolbox = FakeToolbox()
    result = run_agent(
        run_id=run_id,
        task="stuck",
        deps=deps(clean_db, client, toolbox, Budget(max_model_calls=6, repeat_limit=3)),
    )

    # The fourth identical call opens wrap-up instead of ending the run, and the run
    # is recorded as forced rather than voluntary.
    assert result.outcome is Outcome.GAVE_UP
    assert result.wrapup_reason == "repeat:read_file"
    assert result.forced_terminal
    assert toolbox.names == ["read_file", "read_file"], "only the first two are executed"
    # repeat_limit=3 means the third identical call is refused and the fourth ends
    # the run (ADR 006 D6): one repeat is a legitimate retry, a loop is not.
    refused = [
        message
        for message in result.messages
        if message.get("role") == "tool" and "refused: this is the same call" in message["content"]
    ]
    assert len(refused) == 2, "the third and fourth identical calls are both refused"


def test_tool_output_is_truncated_with_a_marker_and_wrapped_as_untrusted(clean_db: Any) -> None:
    body = "\n".join(f"line {n}" for n in range(5_000))
    run_id = str(uuid4())
    client = ScriptedClient(
        [
            turn(call("read_file", path="big.py")),
            turn(call("run_verify")),
            turn(call("finish", summary="ok")),
        ]
    )
    toolbox = FakeToolbox({"read_file": ToolResult(ok=True, content=body)})
    result = run_agent(run_id=run_id, task="read", deps=deps(clean_db, client, toolbox))

    tool_messages = [m for m in result.messages if m.get("role") == "tool"]
    payload = tool_messages[0]["content"]
    assert payload.startswith('<tool_output tool="read_file" untrusted="true">')
    assert "</tool_output>" in payload
    # The budget footer follows the wrapper, so the wrapped output is a prefix.
    assert payload.rstrip().endswith("]")
    assert "…[truncated:" in payload
    assert len(payload.encode("utf-8")) < len(body.encode("utf-8"))
    assert "line 0" in payload and "line 4999" in payload, "head and tail survive"


def test_the_line_budget_is_sized_for_source_not_test_output() -> None:
    """600 lines: a 500-line module arrives in one read, and 601 still truncates.

    The fifth pilot paged a 15 KB file across its whole budget because the cap was
    set for test output. The byte cap alone would not catch a file of short lines,
    so the line cap is asserted at its boundary rather than only by a huge body.
    """
    at_cap = "\n".join(f"line {n}" for n in range(600))
    assert truncate(at_cap) == at_cap
    over_cap = "\n".join(f"line {n}" for n in range(601))
    assert "…[truncated:" in truncate(over_cap)


def test_truncation_leaves_short_output_untouched() -> None:
    assert truncate("short") == "short"
    assert wrap_untrusted("read_file", "x") == (
        '<tool_output tool="read_file" untrusted="true">\nx\n</tool_output>'
    )


def test_a_silent_answer_is_nudged_once_then_enters_wrapup(clean_db: Any) -> None:
    run_id = str(uuid4())
    client = ScriptedClient(
        [
            turn(text="I think it is fine."),
            turn(text="Still fine."),
            turn(call("give_up", reason="no tool call available")),
        ]
    )
    result = run_agent(run_id=run_id, task="chat", deps=deps(clean_db, client, FakeToolbox()))

    # A second silent turn opens wrap-up; the run then ends on a terminal call, and
    # the reason says the protocol had to ask twice.
    assert result.outcome is Outcome.GAVE_UP
    assert result.wrapup_reason == "silent"
    assert result.forced_terminal
    assert any("You must end with a tool call" in m.get("content", "") for m in result.messages)


def test_the_system_prompt_hash_covers_the_version() -> None:
    first = system_prompt_sha256()
    assert len(first) == 64
    import agent.prompts as prompts

    original = prompts.PROMPT_VERSION
    try:
        prompts.PROMPT_VERSION = original + "-next"
        assert prompts.system_prompt_sha256() != first
    finally:
        prompts.PROMPT_VERSION = original


def test_run_result_is_a_value_not_a_state(clean_db: Any) -> None:
    run_id = str(uuid4())
    result: RunResult = run_agent(
        run_id=run_id,
        task="rename",
        deps=deps(clean_db, ScriptedClient(happy_script()), FakeToolbox()),
    )
    assert result.finished
    assert result.wall_seconds >= 0
    assert result.state.sealed


@pytest.mark.parametrize("outcome", [Outcome.FINISHED, Outcome.GAVE_UP, Outcome.BUDGET_EXHAUSTED])
def test_every_outcome_has_a_reported_reason(outcome: Outcome) -> None:
    assert outcome.value in {"finished", "gave_up", "budget_exhausted"}
    assert Outcome.FAILED.value == "failed"


def test_a_no_change_verify_does_not_unlock_finish(clean_db: Any) -> None:
    """A verify that compared nothing cannot be the reason to submit."""
    run_id = str(uuid4())

    class NoChangeToolbox(FakeToolbox):
        def call(self, name: str, arguments: Mapping[str, Any], *, replayed: bool) -> ToolResult:
            if name == "run_verify":
                return ToolResult(
                    ok=False,
                    content="no changes: your candidate is identical to the original source",
                    state="no_changes",
                )
            return super().call(name, arguments, replayed=replayed)

    client = ScriptedClient(
        [
            turn(call("read_file", path="mccabe.py")),
            turn(call("run_verify")),
            turn(call("finish", summary="nothing to do")),
            turn(call("give_up", reason="no change is warranted")),
        ]
    )
    result = run_agent(
        run_id=run_id, task="rename", deps=deps(clean_db, client, NoChangeToolbox())
    )

    assert result.outcome is Outcome.GAVE_UP, result.reason
    refusals = [m["content"] for m in result.messages if m.get("role") == "tool"]
    assert any("identical to the original source" in text for text in refusals), refusals


def test_the_tool_list_shrinks_as_the_budget_runs_down(clean_db: Any) -> None:
    """Wrap-up is protocol, not persuasion: the tools that change the workspace go.

    A mid-run nudge is wording a model can ignore, and capping `apply_patch` would
    fail legitimate work. What it cannot ignore is the tool not being there.
    """
    run_id = str(uuid4())
    seen: list[tuple[tuple[str, ...], str]] = []

    class Recorder(ScriptedClient):
        def complete(self, request: ModelRequest) -> AssistantTurn:
            seen.append(
                (
                    tuple(str(t["function"]["name"]) for t in request.tools),
                    request.tool_choice,
                )
            )
            return super().complete(request)

    # Distinct reads: the same call three times in a row is refused as a repeat.
    client = Recorder(
        [
            turn(call("read_file", path="mccabe.py")),
            turn(call("read_file", path="other.py")),
            turn(call("run_verify")),
            turn(call("run_verify")),
            turn(call("finish", summary="ok")),
        ]
    )
    result = run_agent(
        run_id=run_id,
        task="rename",
        deps=deps(clean_db, client, FakeToolbox(), budget=Budget(max_model_calls=5)),
    )

    full, wrapped, terminal = seen[0][0], seen[2][0], seen[4][0]
    assert "apply_patch" in full and "read_file" in full
    # Three calls left: the workspace-changing tools are gone.
    assert "apply_patch" not in wrapped and "read_file" not in wrapped
    assert {"run_verify", "finish", "give_up"} <= set(wrapped)
    # One call left: only the two that end the run, and a tool call is required.
    assert set(terminal) == {"finish", "give_up"}
    assert seen[4][1] == "required"
    assert result.reached_wrapup


def test_every_tool_result_carries_the_remaining_budget(clean_db: Any) -> None:
    run_id = str(uuid4())
    client = ScriptedClient(
        [
            turn(call("read_file", path="mccabe.py")),
            turn(call("run_verify")),
            turn(call("finish", summary="ok")),
        ]
    )
    result = run_agent(
        run_id=run_id,
        task="rename",
        deps=deps(clean_db, client, FakeToolbox(), budget=Budget(max_model_calls=6)),
    )
    footers = [
        line
        for message in result.messages
        if message.get("role") == "tool"
        for line in str(message["content"]).splitlines()
        if line.startswith("[model calls remaining:")
    ]
    # One per tool result, counting down: the read, the verify, and the finish
    # acknowledgement (which is itself a tool result).
    assert footers == [
        "[model calls remaining: 5]",
        "[model calls remaining: 4]",
        "[model calls remaining: 3]",
    ], footers


def test_a_terminal_call_on_the_last_model_call_is_marked_forced(clean_db: Any) -> None:
    """Wrap-up forcing the decision is budget exhaustion, not a voluntary give_up."""
    run_id = str(uuid4())
    client = ScriptedClient(
        [
            turn(call("read_file", path="mccabe.py")),
            turn(call("run_verify")),
            turn(call("give_up", reason="ran out of room")),
        ]
    )
    result = run_agent(
        run_id=run_id,
        task="rename",
        deps=deps(clean_db, client, FakeToolbox(), budget=Budget(max_model_calls=3)),
    )

    assert result.outcome is Outcome.GAVE_UP
    assert result.reached_wrapup
    assert result.forced_terminal, "the last call was the only one left"


def test_the_token_budget_is_checked_against_the_next_prompt(clean_db: Any) -> None:
    """A budget that only looked at spent tokens bought one more full prompt.

    The probe run measures what the first prompt costs; the second run's cap is that
    plus fifty tokens, and its first turn costs a hundred. Spent alone is under the
    cap, so the old check would have made the next call; spent plus the next prompt is
    over it, so wrap-up opens and the give_up is recorded as forced.
    """
    probe = ScriptedClient(
        [turn(call("read_file", path="mccabe.py")), turn(call("give_up"))]
    )
    run_agent(
        run_id=str(uuid4()),
        task="rename",
        deps=deps(clean_db, probe, FakeToolbox(), Budget(max_model_calls=8)),
    )
    first_prompt = (
        sum(len(str(m.get("content") or "")) for m in probe.requests[0].messages) // 4
    )

    client = ScriptedClient(
        [turn(call("read_file", path="mccabe.py"), tokens=100), turn(call("give_up"))]
    )
    result = run_agent(
        run_id=str(uuid4()),
        task="rename",
        deps=deps(
            clean_db,
            client,
            FakeToolbox(),
            Budget(max_model_calls=8, max_tokens=first_prompt + 50),
        ),
    )

    assert result.outcome is Outcome.GAVE_UP
    assert result.wrapup_reason == "budget:tokens", result.wrapup_reason
    assert result.forced_terminal, "the give_up was made under a budget stop"
    assert result.state.model_calls == 1, "the call that would cross the cap was refused"
    assert "call was refused" in result.reason


def test_a_provider_that_ignores_tool_choice_still_ends_the_run(clean_db: Any) -> None:
    """kimi answers with prose under `tool_choice: "required"` (measured).

    The loop cannot make a provider honour the field, so the last call gets its own
    ending: `failed`, marked forced, which the taxonomy counts as budget exhaustion.
    What must not happen is the budget quietly expiring.
    """
    run_id = str(uuid4())
    client = ScriptedClient(
        [
            turn(call("read_file", path="mccabe.py")),
            turn(text="I have finished thinking about it."),
        ]
    )
    result = run_agent(
        run_id=run_id,
        task="rename",
        deps=deps(clean_db, client, FakeToolbox(), budget=Budget(max_model_calls=2)),
    )

    assert result.outcome is Outcome.FAILED
    assert result.reason.startswith("silent_turn_at_wrapup")
    assert result.forced_terminal
    assert result.reached_wrapup


def test_a_silent_final_call_is_never_turned_into_a_finish(clean_db: Any) -> None:
    """The rule for a forced ending: give_up/wrapup_unresolved, never finish.

    A candidate may only be submitted by the model. When wrap-up's required call comes
    back as prose, the loop has nothing to submit; manufacturing a `finish` would put a
    candidate forward that the model never confirmed. The run ends failed and forced,
    with no terminal tool call in its history.
    """
    run_id = str(uuid4())
    client = ScriptedClient([turn(text="Nothing to do here.")])
    result = run_agent(
        run_id=run_id,
        task="rename",
        deps=deps(clean_db, client, FakeToolbox(), budget=Budget(max_model_calls=1)),
    )

    assert result.outcome is Outcome.FAILED
    assert not result.finished, "the loop submitted a finish the model never made"
    assert result.reason.startswith("silent_turn_at_wrapup"), result.reason
    assert result.forced_terminal
    assert not [m for m in result.messages if m.get("role") == "tool"], (
        "a terminal tool call was manufactured"
    )


def test_the_models_own_finish_on_the_reserved_call_is_accepted(clean_db: Any) -> None:
    """Forced submission is still the model's submission, recorded as forced.

    In wrap-up the patching tools are gone, so an outstanding difference cannot be
    fixed. On the reserved call the loop accepts the `finish` the model actually made
    and sends the candidate to the gate. It records the ending as forced, because the
    protocol, not the model, chose the moment.
    """
    run_id = str(uuid4())
    client = ScriptedClient(
        [
            turn(call("run_verify")),
            turn(call("finish", summary="one difference remains")),
        ]
    )
    result = run_agent(
        run_id=run_id,
        task="rename",
        deps=deps(
            clean_db,
            client,
            FakeToolbox(verify_ok=False),
            Budget(max_model_calls=1, max_wall_seconds=-1.0),
        ),
    )

    assert result.outcome is Outcome.FINISHED
    assert result.reason == "one difference remains"
    assert result.forced_terminal, "the reserved call is a forced ending"
    assert result.finished_open_difference, "the difference was still open at submission"
    assert result.state.wrapup_reason == "budget:wall_clock"


#: One row per way a run used to end without a decision. Each must now open wrap-up
#: and end on a terminal call, with the original reason recorded (ADR 006 D23).
STOP_CONDITIONS = (
    ("tool_cap", "tool_cap:read_file"),
    ("repeat", "repeat:read_file"),
    ("silent", "silent"),
    ("budget:tokens", "budget:tokens"),
    ("budget:wall_clock", "budget:wall_clock"),
)


def _stalled_run(stop: str, clean_db: Any) -> RunResult:
    """Set up each stop condition, then let the model take the forced call."""
    run_id = str(uuid4())
    same = call("read_file", path="a.py")
    terminal = turn(call("give_up", reason="forced"))

    if stop == "tool_cap":
        script = [turn(call("read_file", path=f"f{n}.py")) for n in range(6)] + [terminal]
        budget = Budget.with_tool_slack({"read_file": 1}, max_model_calls=8)
    elif stop == "repeat":
        script = [turn(same), turn(same), turn(same), turn(same), terminal]
        budget = Budget(max_model_calls=8, repeat_limit=3)
    elif stop == "silent":
        script = [turn(text="nothing"), turn(text="still nothing"), terminal]
        budget = Budget(max_model_calls=8)
    elif stop == "budget:tokens":
        script = [turn(call("read_file", path="a.py")), terminal]
        budget = Budget(max_model_calls=8, max_tokens=1)
    elif stop == "budget:wall_clock":
        script = [turn(call("read_file", path="a.py")), terminal]
        budget = Budget(max_model_calls=8, max_wall_seconds=0.0)
    else:  # pragma: no cover - the table is the source of truth
        raise AssertionError(stop)

    return run_agent(
        run_id=run_id,
        task="stall",
        deps=deps(clean_db, ScriptedClient(script), FakeToolbox(), budget),
    )


@pytest.mark.parametrize(
    ("stop", "expected_reason"), STOP_CONDITIONS, ids=[row[0] for row in STOP_CONDITIONS]
)
def test_every_stop_condition_ends_in_a_forced_terminal_call(
    stop: str, expected_reason: str, clean_db: Any
) -> None:
    """A run may end in a terminal call, a provider failure, or nothing at all."""
    result = _stalled_run(stop, clean_db)

    assert result.outcome is Outcome.GAVE_UP, result.reason
    assert result.wrapup_reason == expected_reason, result.wrapup_reason
    assert result.forced_terminal
    assert result.reached_wrapup


def test_a_provider_that_ignores_the_final_required_call_is_recorded(clean_db: Any) -> None:
    """The wrap-up call can still come back as prose (measured: kimi). Then the run
    ends `wrapup_unresolved`, which the taxonomy counts as budget exhaustion."""
    run_id = str(uuid4())
    same = call("read_file", path="a.py")
    client = ScriptedClient(
        [turn(same), turn(same), turn(same), turn(same), turn(text="I am done, really.")]
    )
    result = run_agent(
        run_id=run_id,
        task="stall",
        deps=deps(clean_db, client, FakeToolbox(), Budget(max_model_calls=8, repeat_limit=3)),
    )
    assert result.outcome is Outcome.FAILED
    assert result.reason.startswith("silent_turn_at_wrapup")
    assert result.wrapup_reason == "repeat:read_file"
    assert result.forced_terminal
