"""The agent loop: propose a patch, verify it, and never decide acceptance."""

from .loop import (
    MAX_TOOL_BYTES,
    MAX_TOOL_LINES,
    AgentDeps,
    ModelClient,
    Toolbox,
    run_agent,
    truncate,
    wrap_untrusted,
)
from .prompts import PROMPT_VERSION, SYSTEM_PROMPT, system_prompt_sha256
from .store import CallStore, CommittedTurn, DivergenceError
from .types import (
    FINISH_TOOL,
    GIVE_UP_TOOL,
    PATCH_TOOL,
    VERIFY_TOOL,
    AssistantTurn,
    Budget,
    ModelRequest,
    Outcome,
    RunResult,
    RunState,
    ToolCall,
    ToolResult,
    slack_cap,
)

__all__ = [
    "AgentDeps",
    "AssistantTurn",
    "Budget",
    "CallStore",
    "CommittedTurn",
    "DivergenceError",
    "FINISH_TOOL",
    "GIVE_UP_TOOL",
    "MAX_TOOL_BYTES",
    "MAX_TOOL_LINES",
    "ModelClient",
    "ModelRequest",
    "Outcome",
    "PATCH_TOOL",
    "PROMPT_VERSION",
    "RunResult",
    "RunState",
    "SYSTEM_PROMPT",
    "ToolCall",
    "ToolResult",
    "Toolbox",
    "VERIFY_TOOL",
    "run_agent",
    "slack_cap",
    "system_prompt_sha256",
    "truncate",
    "wrap_untrusted",
]
