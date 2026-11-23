"""Does the provider support `tool_choice: "required"`?

If it does, a model cannot answer in prose: every turn ends in a tool call, and the
silent-turn class disappears as a protocol fact rather than as prompt wording. If it
does not, the loop keeps a one-time nudge and the report says so. Measured once,
recorded, not assumed.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from model_router import PROVIDERS, ModelSpec, OpenAICompatible, load_keys  # noqa: E402

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "reply",
            "description": "Answer the user.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    }
]


def probe(spec: ModelSpec, *, choice: str | None) -> dict[str, object]:
    load_keys()
    config = PROVIDERS[spec.provider]
    client = OpenAICompatible(config, os.environ.get(config.key_env, ""))
    # A prompt that invites prose: with `required` the provider must still return a
    # tool call, which is exactly the guarantee the loop needs.
    result = client.chat(
        spec,
        [{"role": "user", "content": "In one short sentence, what is 2 + 2?"}],
        tools=TOOLS,
        tool_choice=choice,
    )
    return {
        "tool_choice": choice,
        "ok": result.ok,
        "error_type": result.error_type,
        "error_detail": result.error_detail[:200],
        "tool_calls": [call.name for call in result.tool_calls],
        "content": (result.content or "")[:120],
    }


MODELS = (
    ModelSpec(
        provider="numes",
        model_id="nvidia/nemotron-3-ultra-550b-a55b:free",
        label="nemotron-primary",
        max_tokens=256,
    ),
    # The fallback models on the same provider, plus Gemini's compatible endpoint:
    # the loop must not assume that a control which works for one vendor works for
    # all of them.
    ModelSpec(
        provider="numes",
        model_id="moonshotai/kimi-k3.1-pro:free",
        label="kimi-fallback",
        max_tokens=256,
    ),
    ModelSpec(
        provider="gemini",
        model_id="gemini-3.8-flash",
        label="gemini-fallback",
        max_tokens=256,
    ),
)


def main() -> int:
    results: dict[str, object] = {}
    failures = 0
    for spec in MODELS:
        probes = [probe(spec, choice=c) for c in (None, "required", "none")]
        honoured = (
            probes[1]["ok"]
            and bool(probes[1]["tool_calls"])
            and probes[2]["ok"]
            and not probes[2]["tool_calls"]
        )
        results[spec.label] = {"model": spec.model_id, "probes": probes, "honoured": honoured}
        print(
            f"{spec.label:18} auto={bool(probes[0]['tool_calls'])} "
            f"required={bool(probes[1]['tool_calls'])} none={bool(probes[2]['tool_calls'])} "
            f"honoured={honoured}"
        )
        failures += 0 if honoured else 1
    out = REPO_ROOT / "docs/results/tool-choice-probe.json"
    out.write_text(
        json.dumps(results, indent=1, sort_keys=True) + chr(10), encoding="utf-8"
    )
    print("models without tool_choice support:", failures)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
