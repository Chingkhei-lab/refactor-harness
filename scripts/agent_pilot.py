"""Pilot: three tasks, one run each, real model, real verify, real hidden gate.

The pilot exists to find wiring bugs before the approval flow is built, so it is
deliberately small and it reports everything: every run's outcome, its token use,
its verify timing, its tool calls and — where a run went wrong — what the failure
was. Its numbers are **not** the evaluation: three runs cannot estimate a precision,
and the report says so next to them.

Excluded from the headline by construction: the pilot runs are tagged `pilot` in the
results file, and `docs/results/agent-pilot-<date>.md` reports them as wiring
evidence only.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from model_router import PROVIDERS, ModelSpec, OpenAICompatible, load_keys  # noqa: E402

from agent.harness_verify import Golden, HarnessVerifier, file_manifest  # noqa: E402
from agent.live import (  # noqa: E402
    ChatClient,
    WorkspaceToolbox,
    stage_workspace,
)
from agent.loop import AgentDeps, run_agent  # noqa: E402
from agent.store import CallStore  # noqa: E402
from agent.tasks import TASK_DIR, Task, classify  # noqa: E402
from agent.types import Budget  # noqa: E402
from harness.cases import load_cases  # noqa: E402

TREE = REPO_ROOT / ".harness" / "oracle" / "mccabe"
VISIBLE = REPO_ROOT / "harness" / "cases" / "mccabe.json"
HIDDEN = REPO_ROOT / "harness" / "cases" / "mccabe-hidden.json"
RULES = REPO_ROOT / "harness" / "rules" / "mccabe.json"
HIDDEN_RULES = REPO_ROOT / "harness" / "rules" / "mccabe-hidden.json"
GOLDEN = REPO_ROOT / "harness" / "golden"
WORK = REPO_ROOT / ".harness" / "agent-pilot"

PRIMARY = ModelSpec(
    provider="numes",
    model_id="nvidia/nemotron-3-ultra-550b-a55b:free",
    label="nemotron-3-ultra",
    max_tokens=2048,
)

#: Separate tasks, separate directory. The pilot is where the tools and the prompt are
#: tuned, so it must not share a single task with the headline set: three tasks that
#: shaped the prompt cannot then measure it.
PILOT_TASK_DIR = REPO_ROOT / "src" / "agent" / "pilot_tasks"
PILOT_TASKS = ("pilot-with-local", "pilot-docstring", "pilot-no-safe-change")


def _task(task_id: str) -> Task:
    path = PILOT_TASK_DIR / f"{task_id}.json"
    if not path.exists():
        path = TASK_DIR / f"{task_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return Task(
        id=payload["id"],
        kind=payload["kind"],
        target=payload["target"],
        instruction=payload["instruction"],
        allowed_paths=tuple(payload["allowed_paths"]),
        expected=payload["expected"],
        max_model_calls=int(payload.get("max_model_calls", 12)),
        max_files=int(payload.get("max_files", 5)),
        max_lines=int(payload.get("max_lines", 400)),
        allowed_difference_patterns=tuple(payload.get("allowed_difference_patterns") or ()),
        expected_refusal=str(payload.get("expected_refusal") or ""),
        notes=str(payload.get("notes") or ""),
    )


def _case_inputs() -> tuple[str, ...]:
    """Literals a patch must not copy: the visible inputs, verbatim."""
    cases = load_cases(VISIBLE)
    texts: list[str] = []
    for case in cases:
        texts.extend(value for value in case.args if isinstance(value, str) and len(value) >= 3)
        texts.extend(name for name in case.files if len(name) >= 3)
    return tuple(dict.fromkeys(texts))


def _classify(result: Any, task: Task, gate: Any, toolbox: WorkspaceToolbox) -> dict[str, Any]:
    """Score a run with the task set's own ladder, not with a fresh opinion."""
    refusals = [call["code"] for call in toolbox.calls if call.get("code")]
    outcome = classify(
        patch_proposed=any(call["tool"] == "apply_patch" for call in toolbox.calls),
        refusal_codes=refusals,
        verify_different=0,
        suite_failures=0,
        gate_category="" if gate is None else gate.category,
        gave_up_reason=result.reason if str(result.outcome) == "gave_up" else "",
        budget_reason=result.reason if str(result.outcome) == "budget_exhausted" else "",
        forced_terminal=bool(getattr(result, "forced_terminal", False)),
    )
    return {
        "outcome": outcome.outcome,
        "successful": outcome.successful(task),
        "detail": outcome.reason,
    }


def run_one(task: Task, *, model: ModelSpec, run_id: str, client: Any, store: CallStore) -> dict:
    workspace = stage_workspace(TREE, WORK / run_id / "workspace")
    original = file_manifest(TREE)
    visible_verifier = HarnessVerifier(
        source_manifest=original,
        golden=Golden.load(GOLDEN / "mccabe-visible.json"),
        cases=load_cases(VISIBLE),
        rules_path=RULES,
        workspace=workspace,
        workdir=WORK / run_id / "verify",
    )
    hidden_verifier = HarnessVerifier(
        golden=Golden.load(GOLDEN / "mccabe-hidden.json"),
        cases=load_cases(HIDDEN),
        rules_path=HIDDEN_RULES,
        workspace=workspace,
        workdir=WORK / run_id / "hidden",
        source_manifest=original,
    )
    toolbox = WorkspaceToolbox(
        workspace=workspace,
        allowed_prefixes=task.allowed_paths,
        case_inputs=_case_inputs(),
        verifier=visible_verifier,
        hidden_verifier=hidden_verifier,
        allowed_patterns=task.allowed_difference_patterns,
        max_files=task.max_files,
        max_lines=task.max_lines,
        # The pilot measures the wiring, not the oracle: mccabe's hypothesis-driven
        # suite takes minutes per run, so it is "unavailable" here and exercised at
        # gate time in the evaluation, with its own budget.
        suite=(),
    )
    deps = AgentDeps(
        client=ChatClient(client, model, cassette_path=WORK / run_id / "cassette.jsonl"),
        toolbox=toolbox,
        store=store,
        provider=model.provider,
        model=model.model_id,
        budget=Budget.with_tool_slack(
            {
                # The defaults are `slack_cap(1) == 3` per tool, which is right for a
                # scripted test and wrong for a 700-line module: the pilot's first
                # run died at "read_file called 4 times, cap 3" before it had seen
                # the function it was asked to rename. Caps are configuration, so
                # they are set here from what the task needs.
                "read_file": 8,
                "list_files": 2,
                "apply_patch": 4,
                "run_verify": 8,
                "run_tests": 2,
            },
            max_model_calls=task.max_model_calls,
        ),
    )
    started = time.monotonic()
    try:
        result = run_agent(run_id=run_id, task=task.instruction, deps=deps)
        error = ""
    except Exception as exc:  # a pilot reports a failure, it does not hide it
        result = None
        error = f"{type(exc).__name__}: {exc}"
    elapsed = time.monotonic() - started
    patched = any(call["tool"] == "apply_patch" for call in toolbox.calls)
    # No patch, no gate: gating an unmodified tree reports the baseline's own suite
    # state as if it were the patch's verdict. The pilot's first run did exactly
    # that and reported `suite_failure` for a run that wrote nothing.
    gate = toolbox.gate() if result is not None and task.kind != "trap" and patched else None
    # The loop handles `finish` and `give_up` itself, so they never reach the
    # toolbox. A terminal outcome IS a terminal tool call: both are only reachable by
    # calling the tool.
    terminal = [str(result.outcome)] if result is not None and str(result.outcome) in (
        "finished",
        "gave_up",
    ) else []
    record: dict[str, Any] = {
        "reached_wrapup": bool(getattr(result, "reached_wrapup", False)),
        "forced_terminal": bool(getattr(result, "forced_terminal", False)),
        # The pilot's acceptance criterion: a run ends in a terminal TOOL call.
        "terminal_calls": terminal,
        "ended_in_terminal_call": bool(terminal),
        "task": task.id,
        "kind": task.kind,
        "expected": task.expected,
        "run_id": run_id,
        "error": error,
        "elapsed_s": round(elapsed, 1),
        "tool_calls": [call["tool"] for call in toolbox.calls],
        "verify_calls": visible_verifier.calls,
        "verify_seconds": [round(value, 1) for value in visible_verifier.timings],
        "hidden_seconds": [round(value, 1) for value in hidden_verifier.timings],
        "gate": None if gate is None else {"passed": gate.passed, "category": gate.category},
    }
    (WORK / run_id / "tools.jsonl").write_text(
        chr(10).join(json.dumps(call, sort_keys=True) for call in toolbox.calls) + chr(10),
        encoding="utf-8",
    )
    if result is not None:
        record.update(
            {
                "outcome": str(result.outcome),
                "model_calls": result.state.model_calls,
                "replayed": result.state.replayed,
                "tokens": result.state.tokens,
                "summary": result.reason[:600],
                "classification": _classify(result, task, gate, toolbox),
            }
        )
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="docs/results/agent-pilot.json")
    parser.add_argument("--tasks", nargs="*", default=list(PILOT_TASKS))
    parser.add_argument("--model", default=PRIMARY.model_id)
    args = parser.parse_args(argv)

    load_keys()
    config = PROVIDERS[PRIMARY.provider]
    import os

    client = OpenAICompatible(config, os.environ.get(config.key_env, ""))
    database_url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("set TEST_DATABASE_URL (the committed-turn log needs a database)")
    store = CallStore(database_url)
    model = PRIMARY if args.model == PRIMARY.model_id else ModelSpec(
        provider=PRIMARY.provider, model_id=args.model, label=args.model, max_tokens=2048
    )
    runs = []
    for task_id in args.tasks:
        task = _task(task_id)
        # `agent_runs.id` is a uuid: the task id is metadata, not the key.
        run_id = str(uuid4())
        print(f"== {task_id} ({task.kind})")
        record = run_one(task, model=model, run_id=run_id, client=client, store=store)
        print(json.dumps(record, indent=1)[:1200])
        runs.append(record)
    payload = {
        "kind": "pilot",
        "excluded_from_headline": True,
        "target": "mccabe 0.7.0",
        "model": model.model_id,
        "visible_case_set_hash": Golden.load(GOLDEN / "mccabe-visible.json").case_set_hash,
        "hidden_case_set_hash": Golden.load(GOLDEN / "mccabe-hidden.json").case_set_hash,
        "runs": runs,
        "total_tokens": sum(int(run.get("tokens") or 0) for run in runs),
        "total_elapsed_s": round(sum(float(run["elapsed_s"]) for run in runs), 1),
    }
    out = REPO_ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {out.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
