"""The cassette has to answer what was sent and how the provider stopped."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent.live import ChatClient
from agent.types import ModelRequest


class FakeClient:
    """One provider call, shaped like scripts/model_router.ChatResult."""

    def __init__(self) -> None:
        self.seen: dict[str, Any] = {}

    def chat(
        self,
        model: Any,
        messages: list[dict[str, Any]],
        *,
        tools: Any = None,
        tool_choice: Any = None,
        temperature: float = 0.0,
    ) -> Any:
        self.seen = {"tool_choice": tool_choice, "tools": tools}
        result = type("Result", (), {})()
        result.ok = True
        result.error_type = ""
        result.error_detail = ""
        result.content = ""
        result.tool_calls = ()
        result.usage = {"prompt_tokens": 10, "completion_tokens": 2}
        result.latency_s = 0.1
        result.attempts = 1
        result.reasoning = "checked the request"
        result.finish_reason = "tool_calls"
        return result


def request() -> ModelRequest:
    tools = (
        {"type": "function", "function": {"name": "finish"}},
        {"type": "function", "function": {"name": "give_up"}},
    )
    return ModelRequest(
        messages=({"role": "user", "content": "go"},),
        tools=tools,
        temperature=0.0,
        system_prompt_version="v",
        tool_choice="required",
    )


def test_the_cassette_records_what_was_sent_and_how_the_call_ended(tmp_path: Path) -> None:
    client = FakeClient()
    model = type("Model", (), {"provider": "p", "model_id": "m"})()
    cassette = tmp_path / "cassette.jsonl"
    chat = ChatClient(client, model, cassette_path=cassette, tool_choice="none")

    chat.complete(request())

    record = json.loads(cassette.read_text(encoding="utf-8").strip())
    assert client.seen["tool_choice"] == "required", "the request must win over the default"
    assert record["tool_choice"] == "required"
    assert record["tools"] == ["finish", "give_up"]
    assert record["finish_reason"] == "tool_calls"
    assert record["reasoning"] == "checked the request"
