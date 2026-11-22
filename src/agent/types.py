"""Agent run identity, model turns, budgets and the outcome vocabulary.

The vocabulary is deliberately small and closed: every run ends in exactly one
`Outcome`, and every outcome is reported. `budget_exhausted` and `gave_up` are
results, not exceptions — an agent that stops is a measurement of where the loop
gave up, and hiding that behind a raised exception loses the most interesting
category in the evaluation (ADR 006 D5, D10).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Outcome(StrEnum):
    """How a run ended. `FINISHED` means "submitted", not "accepted"."""

    FINISHED = "finished"
    GAVE_UP = "gave_up"
    BUDGET_EXHAUSTED = "budget_exhausted"
    FAILED = "failed"


FINISH_TOOL = "finish"
GIVE_UP_TOOL = "give_up"
VERIFY_TOOL = "run_verify"
PATCH_TOOL = "apply_patch"
READ_TOOLS = frozenset({"read_file", "list_files"})


def canonical_json(value: Any) -> str:
    """One encoding for hashing: sorted keys, no spaces, ASCII-safe."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: Mapping[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {"id": self.call_id, "name": self.name, "arguments": dict(self.arguments)}

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> ToolCall:
        return cls(
            call_id=str(raw["id"]),
            name=str(raw["name"]),
            arguments=dict(raw.get("arguments") or {}),
        )

    def signature(self) -> str:
        """Identity for repeat detection: same tool, same arguments."""
        return sha256_text(canonical_json({"name": self.name, "args": self.arguments}))


@dataclass(frozen=True)
class AssistantTurn:
    """One model response, exactly as it will be replayed.

    `raw` is the provider payload with the assistant message intact. It is kept
    because a provider may require its own fields to be echoed back on the next
    turn — Gemini 3.x returns `thought_signature` and answers 400 without it — so
    a replay that reconstructed the message from our own fields would break.
    """

    text: str
    tool_calls: tuple[ToolCall, ...]
    provider: str
    model: str
    tokens_prompt: int | None
    tokens_completion: int | None
    latency_ms: int
    attempts: int
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def tokens(self) -> int:
        return (self.tokens_prompt or 0) + (self.tokens_completion or 0)

    def to_json(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "tool_calls": [call.to_json() for call in self.tool_calls],
            "provider": self.provider,
            "model": self.model,
            "tokens_prompt": self.tokens_prompt,
            "tokens_completion": self.tokens_completion,
            "latency_ms": self.latency_ms,
            "attempts": self.attempts,
            "raw": dict(self.raw),
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> AssistantTurn:
        return cls(
            text=str(raw.get("text") or ""),
            tool_calls=tuple(
                ToolCall.from_json(item) for item in raw.get("tool_calls") or ()
            ),
            provider=str(raw.get("provider") or ""),
            model=str(raw.get("model") or ""),
            tokens_prompt=raw.get("tokens_prompt"),
            tokens_completion=raw.get("tokens_completion"),
            latency_ms=int(raw.get("latency_ms") or 0),
            attempts=int(raw.get("attempts") or 1),
            raw=dict(raw.get("raw") or {}),
        )


def slack_cap(expected: int) -> int:
    """A per-tool cap with slack.

    The bake-off is the reason this is a function rather than a constant: a hard
    4-call cap failed every model on tasks that legitimately need five calls, so
    the cap is "what the task needs, plus room to recover". Refusing exploration is
    not what a budget is for (ADR 006 D5).
    """
    return expected + max(2, math.ceil(expected / 2))


@dataclass(frozen=True)
class Budget:
    max_model_calls: int = 12
    max_tokens: int = 120_000
    max_wall_seconds: float = 900.0
    tool_caps: Mapping[str, int] = field(default_factory=dict)
    repeat_limit: int = 3

    @classmethod
    def with_tool_slack(cls, expected: Mapping[str, int], **kwargs: Any) -> Budget:
        return cls(tool_caps={name: slack_cap(n) for name, n in expected.items()}, **kwargs)

    def tool_cap(self, name: str) -> int:
        return self.tool_caps.get(name, slack_cap(1))

    def to_json(self) -> dict[str, Any]:
        return {
            "max_model_calls": self.max_model_calls,
            "max_tokens": self.max_tokens,
            "max_wall_seconds": self.max_wall_seconds,
            "tool_caps": dict(self.tool_caps),
            "repeat_limit": self.repeat_limit,
        }


@dataclass(frozen=True)
class ModelRequest:
    """What we send. The hash of this is the replay precondition."""

    messages: tuple[dict[str, Any], ...]
    tools: tuple[dict[str, Any], ...]
    model: str
    temperature: float
    system_prompt_version: str

    def sha256(self) -> str:
        return sha256_text(
            canonical_json(
                {
                    "messages": list(self.messages),
                    "tools": list(self.tools),
                    "model": self.model,
                    "temperature": self.temperature,
                    "system_prompt_version": self.system_prompt_version,
                }
            )
        )


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    content: str
    effect: str = "read"  # "read" or "write"


@dataclass
class RunState:
    """Mutable accounting for one run attempt."""

    run_id: str
    turn: int = 0
    model_calls: int = 0
    replayed: int = 0
    tokens: int = 0
    tool_counts: dict[str, int] = field(default_factory=dict)
    read_paths: set[str] = field(default_factory=set)
    verified: bool = False
    last_verify_ok: bool | None = None
    last_signature: str | None = None
    repeats: int = 0
    sealed: bool = False

    def note_tool(self, name: str) -> int:
        self.tool_counts[name] = self.tool_counts.get(name, 0) + 1
        return self.tool_counts[name]

    def note_signature(self, signature: str, limit: int) -> bool:
        """Count consecutive identical tool calls. True if the call is refused."""
        if signature == self.last_signature:
            self.repeats += 1
        else:
            self.repeats = 1
            self.last_signature = signature
        return self.repeats >= limit


@dataclass(frozen=True)
class RunResult:
    outcome: Outcome
    reason: str
    state: RunState
    wall_seconds: float
    messages: tuple[dict[str, Any], ...]

    @property
    def finished(self) -> bool:
        return self.outcome is Outcome.FINISHED
