"""Step 1 of the model bake-off: does this model emit a usable tool call?

One request with a `read_file(path)` tool and a prompt that cannot be answered
without calling it, then a second turn carrying the tool result. A model passes
only if both turns work:

1. turn 1 returns a structured tool call (not JSON in prose) whose `arguments`
   parse as an object with the expected key, and
2. turn 2, given `role="tool"`, produces a final answer that uses the content and
   does not invent another call.

Everything is recorded under `.harness/bakeoff/cassettes/smoke/<model>.json` so the
exact shape can be quoted in the report without re-running anything.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from model_router import (  # noqa: E402
    ChatResult,
    ModelSpec,
    assistant_message,
    build,
    redact,
)

REPO_ROOT = SCRIPT_DIR.parent
OUT_DIR = REPO_ROOT / ".harness" / "bakeoff"

FINALISTS: list[ModelSpec] = [
    ModelSpec("numes", "nvidia/nemotron-3-ultra-550b-a55b:free", "Nemotron 3 Ultra (availability)"),
    ModelSpec(
        "numes", "deepseek/deepseek-v4.1-flash:free", "DeepSeek V4.1 Flash (generation speed)"
    ),
    ModelSpec("numes", "moonshotai/kimi-k3.1-pro:free", "Kimi K3.1 Pro (capability guess)"),
]

READ_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a UTF-8 text file from the workspace and return its lines.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to the workspace root, e.g. 'calc/core.py'.",
                }
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
}

FILE_CONTENT = (
    "1: def add(a, b):\n"
    "2:     return a + b\n"
    "3: \n"
    "4: \n"
    "5: def mean(values):\n"
    "6:     return sum(values) / len(values)\n"
)

SYSTEM = (
    "You are a coding agent working in a small Python repository. You must call a tool "
    "to inspect files; you cannot see the repository otherwise. Prefer one tool call "
    "over guessing."
)
QUESTION = (
    "Read calc/core.py and tell me the exact name of the function that computes the "
    "average. Use the read_file tool."
)


def _shape(result: ChatResult) -> dict[str, object]:
    """The response shape, which is what the report has to show."""
    choices = result.raw.get("choices") or []
    message = (choices[0].get("message") or {}) if choices else {}
    return {
        "top_level_keys": sorted(result.raw.keys()),
        "message_keys": sorted(message.keys()),
        "tool_calls_type": type(message.get("tool_calls")).__name__,
        "arguments_type": (
            type(
                ((message.get("tool_calls") or [{}])[0].get("function") or {}).get("arguments")
            ).__name__
            if message.get("tool_calls")
            else None
        ),
        "finish_reason": result.finish_reason,
    }


def smoke(model: ModelSpec) -> dict[str, object]:
    provider = build(model.provider)
    turned_on = time.perf_counter()
    turn1 = provider.chat(
        model,
        [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": QUESTION},
        ],
        tools=[READ_FILE_TOOL],
        tool_choice="auto",
    )
    record: dict[str, object] = {
        "model": model.model_id,
        "provider": model.provider,
        "label": model.label,
        "temperature": 0.0,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "turn1": {
            "ok": turn1.ok,
            "error_type": turn1.error_type,
            "error_detail": turn1.error_detail,
            "latency_s": round(turn1.latency_s, 2),
            "attempts": turn1.attempts,
            "content": turn1.content,
            "reasoning_chars": len(turn1.reasoning),
            "tool_calls": [
                {
                    "id": call.id,
                    "name": call.name,
                    "arguments_raw": call.arguments_raw,
                    "arguments": call.arguments,
                    "argument_error": call.argument_error,
                }
                for call in turn1.tool_calls
            ],
            "usage": turn1.usage,
            "finish_reason": turn1.finish_reason,
            "shape": _shape(turn1),
            "raw_message": redact(
                (turn1.raw.get("choices") or [{}])[0].get("message") or {}
            ),
        },
    }

    call = turn1.tool_calls[0] if turn1.tool_calls else None
    turn1_ok = bool(
        turn1.ok
        and len(turn1.tool_calls) == 1
        and call is not None
        and call.name == "read_file"
        and call.arguments is not None
        and isinstance(call.arguments.get("path"), str)
        and call.arguments.get("path") == "calc/core.py"
    )
    record["turn1_pass"] = turn1_ok

    turn2: ChatResult | None = None
    turn2_ok = False
    if turn1_ok and call is not None:
        assert call.arguments is not None
        assistant = assistant_message(turn1)
        turn2 = provider.chat(
            model,
            [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": QUESTION},
                assistant,
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": FILE_CONTENT,
                },
            ],
            tools=[READ_FILE_TOOL],
            tool_choice="auto",
        )
        turn2_ok = bool(
            turn2.ok
            and "mean" in turn2.content.lower()
            and not turn2.tool_calls
        )
        record["turn2"] = {
            "ok": turn2.ok,
            "error_type": turn2.error_type,
            "error_detail": turn2.error_detail,
            "latency_s": round(turn2.latency_s, 2),
            "attempts": turn2.attempts,
            "content": turn2.content,
            "reasoning_chars": len(turn2.reasoning),
            "tool_calls": [
                {"name": c.name, "arguments_raw": c.arguments_raw} for c in turn2.tool_calls
            ],
            "usage": turn2.usage,
            "finish_reason": turn2.finish_reason,
            "shape": _shape(turn2),
        }
        record["turn2_pass"] = turn2_ok

    ttft = provider.time_to_first_token(
        model, [{"role": "user", "content": "Say OK."}], max_tokens=8
    )
    record["ttft"] = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in ttft.items()}
    record["total_seconds"] = round(time.perf_counter() - turned_on, 1)
    record["pass"] = bool(turn1_ok and turn2_ok)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--models",
        default="",
        help="comma-separated keys from model_bakeoff.MODELS; default is the three finalists",
    )
    args = parser.parse_args(argv)
    chosen = list(FINALISTS)
    if args.models:
        from model_bakeoff import MODELS

        chosen = [MODELS[key.strip()] for key in args.models.split(",") if key.strip()]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    records = []
    for model in chosen:
        print(f"--- {model.model_id}")
        try:
            record = smoke(model)
        except Exception as exc:  # noqa: BLE001 - the point is to record, not to crash
            record = {
                "model": model.model_id,
                "label": model.label,
                "pass": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        records.append(record)
        path = OUT_DIR / "cassettes" / "smoke" / f"{model.slug}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
        turn1 = record.get("turn1") or {}
        print(
            f"    pass={record.get('pass')} turn1={bool(record.get('turn1_pass'))} "
            f"turn2={bool(record.get('turn2_pass'))} "
            f"latency={turn1.get('latency_s')}s ttft={((record.get('ttft') or {}).get('ttft_s'))}"
        )
        call = (turn1.get("tool_calls") or [{}])[0]
        if call:
            print(f"    tool_call={json.dumps(call, ensure_ascii=False)[:300]}")

    # Merge by model id: the finalists and the provider fallback are smoke-tested
    # in separate runs, and a later run must not erase an earlier result.
    merged_path = OUT_DIR / "smoke.json"
    merged: dict[str, dict[str, object]] = {}
    if merged_path.exists():
        for entry in json.loads(merged_path.read_text(encoding="utf-8")):
            merged[str(entry.get("model"))] = entry
    for entry in records:
        merged[str(entry.get("model"))] = entry
    merged_path.write_text(
        json.dumps(list(merged.values()), indent=2, default=str), encoding="utf-8"
    )
    survivors = [r for r in records if r.get("pass")]
    print(f"\n{len(survivors)}/{len(records)} pass the smoke test")
    return 0 if survivors else 1


if __name__ == "__main__":
    raise SystemExit(main())
