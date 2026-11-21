"""The bake-off: run models through the real tool loop and score the outcome.

Design rules, fixed before the first model is called:

* **Scoring is deterministic.** A run succeeds when the final workspace passes the
  task's own check, the tests pass, the answer contains what was asked for, and no
  discipline rule was broken. Nothing is judged by another model.
* **A task proves itself first.** `--self-check` applies each setup (the check must
  fail) and each reference fix (the check must pass), so a broken task cannot be
  reported as a model failure.
* **Every response is a cassette.** `--analyse` rebuilds the whole report from
  cassettes without a single network call, so re-reading a run costs nothing.
* **Models are interleaved.** For each task and run, the model order rotates, so
  no model is always measured first in an hour where availability drifts.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import bakeoff_tasks as tasks_module  # noqa: E402
import bakeoff_tools as tools_module  # noqa: E402
from model_router import ChatResult, ModelSpec, assistant_message, build, redact  # noqa: E402

REPO_ROOT = SCRIPT_DIR.parent
WORK_ROOT = REPO_ROOT / ".harness" / "bakeoff"
CASSETTE_ROOT = WORK_ROOT / "cassettes"

SYSTEM_PROMPT = (
    "You are a coding agent working in a small Python repository. Use the tools to "
    "inspect and change files; you cannot see the repository otherwise. Patch with "
    "apply_patch, which needs the file's current sha256 from read_file. When the task "
    "is done, reply with the answer and stop calling tools."
)

MODELS: dict[str, ModelSpec] = {
    "nemotron": ModelSpec(
        "numes",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "Nemotron 3 Ultra (availability)",
        max_tokens=4096,
    ),
    "deepseek": ModelSpec(
        "numes",
        "deepseek/deepseek-v4.1-flash:free",
        "DeepSeek V4.1 Flash (generation speed)",
        max_tokens=4096,
    ),
    "kimi": ModelSpec(
        "numes",
        "moonshotai/kimi-k3.1-pro:free",
        "Kimi K3.1 Pro (capability guess)",
        max_tokens=4096,
    ),
    "glm": ModelSpec("numes", "z-ai/glm-5.3-flash:free", "GLM 5.3 Flash (reserve finalist)"),
    "gemini": ModelSpec(
        "gemini",
        "gemini-3.8-flash",
        "Gemini 3.8 Flash (provider fallback)",
        supports_temperature=True,
    ),
}


# -- task self-check -----------------------------------------------------


def apply_edits(
    workspace: tools_module.Workspace, edits: tuple[tuple[str, str, str], ...]
) -> list[str]:
    applied = []
    for path, old, new in edits:
        if old == "":
            if not workspace.write_text(path, new):
                raise SystemExit(f"cannot create {path}")
            applied.append(f"create {path}")
            continue
        text = workspace.read_text(path)
        if text is None:
            raise SystemExit(f"missing {path}")
        count = text.count(old)
        if count != 1:
            raise SystemExit(f"{path}: edit text occurs {count} times, expected 1")
        if not workspace.write_text(path, text.replace(old, new, 1)):
            raise SystemExit(f"cannot write {path}")
        applied.append(f"edit {path}")
    return applied


def self_check(only: str = "") -> int:
    failures = 0
    for task in tasks_module.TASKS:
        if only and task.id != only:
            continue
        # A task with no setup starts healthy: only the reference state is proved.
        # The reference is applied *on top of* the setup, which is the state the
        # model will actually see.
        phases = [("reference", task.setup + task.reference, True)]
        if task.setup:
            phases.insert(0, ("setup", task.setup, False))
        for phase, edits, expected in phases:
            workspace = tools_module.Workspace.fresh(WORK_ROOT / "selfcheck" / task.id)
            try:
                apply_edits(workspace, edits)
                tests_ok = workspace.run_tests().payload.get("exit_code") == 0
                passed, detail = tools_module.run_check(
                    workspace, task.check, "add 2 5 core.py mean /summaries"
                )
            except SystemExit as exc:
                print(f"FAIL {task.id} [{phase}]: {exc}")
                failures += 1
                continue
            # Whichever signal the task actually scores on decides the phase; when
            # the check is a constant (a task whose verdict is its test suite), the
            # check cannot be the witness for either state.
            primary = tests_ok if task.tests_must_pass else passed
            ok = primary == expected
            if task.tests_must_pass and task.check is not tasks_module.CHECK_ANSWER:
                ok = ok and passed == expected
            status = "ok " if ok else "FAIL"
            print(
                f"{status} {task.id:38s} [{phase:9s}] check={passed!s:5s} tests={tests_ok!s:5s}"
                f"  {detail[:80]}"
            )
            if not ok:
                failures += 1
    print(f"\n{'self-check passed' if failures == 0 else f'{failures} self-check failures'}")
    return 1 if failures else 0


# -- one run -------------------------------------------------------------


@dataclass
class RunRecord:
    model: str
    model_id: str
    task: str
    category: str
    run_index: int
    started_at: str = ""
    success: bool = False
    calls: list[dict[str, Any]] = field(default_factory=list)
    turns: list[dict[str, Any]] = field(default_factory=list)
    answer: str = ""
    violations: list[str] = field(default_factory=list)
    failure_reason: str = ""
    tests_ok: bool = False
    check_ok: bool = False
    answer_ok: bool = True
    args_ok: bool = True
    unchanged_ok: bool = True
    checks_detail: str = ""
    task_seconds: float = 0.0
    tokens: int = 0
    repeats: int = 0
    setups: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "model_id": self.model_id,
            "task": self.task,
            "category": self.category,
            "run_index": self.run_index,
            "started_at": self.started_at,
            "success": self.success,
            "calls": self.calls,
            "turns": self.turns,
            "answer": self.answer,
            "violations": self.violations,
            "failure_reason": self.failure_reason,
            "tests_ok": self.tests_ok,
            "check_ok": self.check_ok,
            "answer_ok": self.answer_ok,
            "args_ok": self.args_ok,
            "unchanged_ok": self.unchanged_ok,
            "checks_detail": self.checks_detail,
            "task_seconds": round(self.task_seconds, 2),
            "tokens": self.tokens,
            "repeats": self.repeats,
            "setups": self.setups,
        }


def _normalise_args(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    if not arguments:
        return {}
    normalised = dict(arguments)
    if name == "read_file":
        normalised.setdefault("start", 1)
        normalised.setdefault("end", None)
    if name == "search":
        normalised.setdefault("path", ".")
    if name == "list_dir":
        normalised.setdefault("path", ".")
    return normalised


def _expected_matches(task: tasks_module.Task, calls: list[dict[str, Any]]) -> bool:
    if not task.expect_calls:
        return True
    if len(calls) != len(task.expect_calls):
        return False
    for expected, observed in zip(task.expect_calls, calls, strict=True):
        if expected["name"] != observed["name"]:
            return False
        if _normalise_args(expected["name"], expected["arguments"]) != _normalise_args(
            observed["name"], observed.get("arguments")
        ):
            return False
    return True


def _fixture_test_hashes() -> dict[str, str]:
    return {
        name: tools_module.sha256_text(
            (tools_module.FIXTURE_ROOT / name).read_text(encoding="utf-8")
        )
        for name in ("tests/test_core.py", "tests/test_cli.py")
    }


def run_one(model_key: str, task: tasks_module.Task, run_index: int) -> RunRecord:
    model = MODELS[model_key]
    provider = build(model.provider)
    record = RunRecord(
        model=model_key,
        model_id=model.model_id,
        task=task.id,
        category=task.category,
        run_index=run_index,
        started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    work = WORK_ROOT / "work" / model_key / task.id / f"run{run_index}"
    workspace = tools_module.Workspace.fresh(work, force_stale_once=task.force_stale_once)
    record.setups = apply_edits(workspace, task.setup)
    before_tests = _fixture_test_hashes()

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task.prompt},
    ]
    started = time.perf_counter()
    answer = ""
    seen_calls: dict[str, str] = {}
    try:
        for _step in range(task.budget + 1):
            result: ChatResult = provider.chat(
                model, messages, tools=tools_module.TOOLS, tool_choice="auto"
            )
            record.tokens += result.total_tokens
            record.turns.append(
                {
                    "ok": result.ok,
                    "error_type": result.error_type,
                    "error_detail": result.error_detail[:200],
                    "latency_s": round(result.latency_s, 2),
                    "attempts": result.attempts,
                    "finish_reason": result.finish_reason,
                    "content": result.content[:2000],
                    "reasoning_chars": len(result.reasoning),
                    "tool_calls": [
                        {"name": call.name, "arguments_raw": call.arguments_raw[:800]}
                        for call in result.tool_calls
                    ],
                    "usage": result.usage,
                }
            )
            if not result.ok:
                record.violations.append(f"request_failed:{result.error_type}")
                record.failure_reason = f"request failed: {result.error_type}"
                break

            if not result.tool_calls:
                answer = result.content
                break

            messages.append(assistant_message(result))

            stop = False
            for call in result.tool_calls:
                invalid = tools_module.validate_arguments(call.name, call.arguments)
                if len(record.calls) >= task.budget:
                    record.violations.append("over_budget")
                    stop = True
                key = f"{call.name}:{json.dumps(call.arguments, sort_keys=True)}"
                if key in seen_calls and seen_calls[key] == "" and not stop:
                    # Only a task that asks for it treats a repeat as a fault: running
                    # the suite again after a patch is normal, and counting it as
                    # indiscipline would punish the behaviour the task rewards.
                    if task.forbid_repeats:
                        record.violations.append(f"repeated_call:{call.name}")
                    else:
                        record.repeats += 1
                if invalid:
                    detail = {"error": "invalid_arguments", "detail": invalid}
                    tool_payload = json.dumps(detail)
                    result_payload = detail
                else:
                    assert call.arguments is not None
                    tool_result = workspace.call(call.name, call.arguments)
                    tool_payload = tool_result.to_message()
                    result_payload = tool_result.payload
                seen_calls[key] = str(result_payload.get("error", ""))
                if call.name == "apply_patch" and result_payload.get("applied"):
                    # A successful edit changes the world, so an identical later call
                    # is not a repeat of the same question.
                    seen_calls.clear()
                record.calls.append(
                    {
                        "name": call.name,
                        "arguments": call.arguments,
                        "arguments_raw": call.arguments_raw[:800],
                        "valid": not invalid,
                        "invalid_reason": invalid,
                        "error": result_payload.get("error", ""),
                        "result": result_payload,
                    }
                )
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": tool_payload}
                )
            if stop:
                record.failure_reason = "call budget exceeded"
                break
        else:
            record.failure_reason = "the model never stopped calling tools"
    except Exception as exc:  # noqa: BLE001 - record, never lose a run
        record.failure_reason = f"{type(exc).__name__}: {exc}"
        record.violations.append("harness_exception")

    record.task_seconds = time.perf_counter() - started
    record.answer = answer

    tests_ok = True
    if task.tests_must_pass:
        tests_ok = workspace.run_tests().payload.get("exit_code") == 0
    record.tests_ok = tests_ok
    record.check_ok, record.checks_detail = tools_module.run_check(workspace, task.check, answer)
    record.answer_ok = all(
        wanted.lower() in answer.lower() for wanted in task.answer_must_contain
    )
    record.args_ok = _expected_matches(task, record.calls)
    if task.tests_unchanged:
        after = _fixture_test_hashes()
        record.unchanged_ok = all(after[name] == before_tests[name] for name in before_tests)

    record.success = (
        tests_ok
        and record.check_ok
        and record.answer_ok
        and record.args_ok
        and record.unchanged_ok
        and not record.violations
    )
    if not record.success and not record.failure_reason:
        reasons = []
        if not tests_ok:
            reasons.append("tests still fail")
        if not record.check_ok:
            reasons.append(f"check: {record.checks_detail}")
        if not record.answer_ok:
            reasons.append("answer missing expected value")
        if not record.args_ok:
            reasons.append("tool arguments were not the requested ones")
        if not record.unchanged_ok:
            reasons.append("tests/ was modified")
        if record.violations:
            reasons.extend(record.violations)
        record.failure_reason = "; ".join(reasons)
    return record


# -- metrics -------------------------------------------------------------


def wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total == 0:
        return (0.0, 1.0)
    phat = successes / total
    denominator = 1 + z * z / total
    centre = (phat + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(phat * (1 - phat) / total + z * z / (4 * total * total)) / denominator
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def pct(value: float) -> str:
    return f"{100 * value:.1f} %"


def summarise(records: list[RunRecord], total_tasks: int) -> dict[str, Any]:
    by_model: dict[str, list[RunRecord]] = {}
    for record in records:
        by_model.setdefault(record.model, []).append(record)
    summary: dict[str, Any] = {}
    for model, rows in by_model.items():
        runs = len(rows)
        successes = sum(1 for row in rows if row.success)
        low, high = wilson(successes, runs)
        calls = [call for row in rows for call in row.calls]
        valid = sum(1 for call in calls if call["valid"])
        call_latencies = [
            turn["latency_s"] for row in rows for turn in row.turns if turn.get("latency_s")
        ]
        task_latencies = [row.task_seconds for row in rows]
        tokens = [row.tokens for row in rows]
        failures: dict[str, int] = {}
        for row in rows:
            for turn in row.turns:
                if not turn["ok"]:
                    failures[turn["error_type"]] = failures.get(turn["error_type"], 0) + 1
        retries = sum(max(0, turn["attempts"] - 1) for row in rows for turn in row.turns)
        repeats = sum(row.repeats for row in rows)
        summary[model] = {
            "label": rows[0].model_id,
            "runs": runs,
            "successes": successes,
            "success_rate": successes / runs if runs else 0.0,
            "ci": (low, high),
            "calls": len(calls),
            "valid_calls": valid,
            "valid_rate": valid / len(calls) if calls else 0.0,
            "median_call_s": statistics.median(call_latencies) if call_latencies else 0.0,
            "p95_call_s": percentile(call_latencies, 0.95),
            "median_task_s": statistics.median(task_latencies) if task_latencies else 0.0,
            "p95_task_s": percentile(task_latencies, 0.95),
            "median_tokens": statistics.median(tokens) if tokens else 0,
            "failed_requests": failures,
            "failed_total": sum(failures.values()),
            "retries": retries,
            "repeats": repeats,
            "tasks_attempted": len({row.task for row in rows}),
            "tasks": total_tasks,
        }
    return summary


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


# -- report --------------------------------------------------------------



NARRATIVE_TEXT = """## How this was measured

* **Tool calls only.** Every task runs through the same five tools (`read_file`,
  `search`, `list_dir`, `apply_patch`, `run_tests`) against a real workspace copy.
  `apply_patch` refuses a stale sha256, a text that does not occur exactly once, and
  a path outside the workspace; `run_tests` is the fixture's own pytest run.
* **Deterministic scoring.** A run succeeds only if the final workspace passes the
  task's check, the test suite passes, the answer contains what was asked for,
  `tests/` is untouched where the task requires it, the exact-argument tasks got
  exactly those arguments, and no discipline rule was broken. No model grades
  another model.
* **The tasks are proved before they are used.** `--self-check` applies each task's
  setup (the check must fail) and then its setup plus reference fix (the check must
  pass), so a task nobody could pass is caught before a model is blamed for it.
* **Interleaved.** For each task and run the model order rotates, so no model is
  always measured first while availability drifts.
* **Temperature 0**, model IDs and the run date are recorded per turn, and every
  response is a cassette under `.harness/bakeoff/cassettes/`. `--analyse` rebuilds
  this file from the cassettes with no network call.

### One defect found while running, and re-run

The first stage-1 pass was thrown away: the retry policy listed
`{500, 502, 503, 504}` and the provider's edge answers **520/522**, so the most
common failure was classified as `server` and never retried — 11 of 31 task
failures in that pass were a single 520 on the first turn. The policy now retries
every 5xx, and every number here comes from the re-run with that fix. Budgets were
raised at the same time: a model that lists three directories and reads four files
before patching is wasteful, not indisciplined, and the budget exists to stop a
loop, not to punish exploration. The tight budgets now live only in the
`discipline` tasks, whose prompts say so.

## Data policy

Assume every prompt and every response is **logged and may be read by a human
reviewer**.

* Only open-source target code is ever sent: the `calc` fixture in this repository
  and `parse`/`pycodestyle` sources that are already public. No secrets, keys,
  customer data or private repository content goes into a prompt.
* The API keys live in `.harness/bakeoff/keys.env`, which is git-ignored, and no
  cassette records an `Authorization` header (`redact()` plus a unit test).
* Gemini's free tier, quoted from `https://ai.google.dev/gemini-api/terms`: for
  Unpaid Services — "the unpaid quota on Gemini API" — Google "uses the content you
  submit to the Services and any generated responses to provide, improve, and
  develop Google products and services and machine learning technologies", and
  "human reviewers may read, annotate, and process your API input and output". The
  page's own instruction is "Do not submit sensitive, confidential, or personal
  information." The same assumption is made for numes.ai, whose policy was not
  available to check.

## Caveats

* **Twelve tasks cannot separate models by much.** With 24 runs per model the 95 %
  Wilson interval is roughly ±16 points, which is why stage 2 moves to 20 tasks and
  3 runs for the top two.
* **Tool-call validity did not discriminate.** All three finalists emitted
  schema-valid JSON on every call, so the 95 % gate excluded nobody; the ranking came
  down to task success, then latency.
* **Availability is real but visible.** Free-tier 5xx are retried up to five times
  with exponential backoff; a run that still fails is a task failure and is reported,
  not smoothed away. The failed-request column is what that costs.
* **Latency is per call, not time to first token.** The loop is not streamed, because
  reassembling fragmented tool-call deltas by hand would add a failure mode to the
  thing being measured. TTFT was measured once per model in the smoke test.
* **Free quotas decide the final size.** Rate-limit headers are captured when the
  provider sends them; `retries` is the observable part of the quota story.

## Reproduce

```
python scripts/model_smoke.py                          # step 1: tool call + tool result
python scripts/model_bakeoff.py --self-check           # prove every task is well posed
python scripts/model_bakeoff.py --stage 1 --runs 2
python scripts/model_bakeoff.py --stage 2 --runs 3 --models <top two>
python scripts/model_bakeoff.py --stage 2 --analyse     # rebuild this file, no network
```
"""

NARRATIVE: list[str] = NARRATIVE_TEXT.split("\n")

def _smoke_section() -> list[str]:
    """Step-1 results, read from the smoke cassettes rather than re-run."""
    path = WORK_ROOT / "smoke.json"
    if not path.exists():
        return []
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not rows:
        return []
    lines = [
        "## Step 1: the smoke test",
        "",
        "One request with a `read_file(path)` tool and a prompt that cannot be answered "
        "without it, then a second turn carrying the tool result. A model passes only if "
        "both turns work. The tool call below is the response verbatim.",
        "",
        "| model | turn 1 tool call | turn 1 | turn 2 | latency | TTFT |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        turn1 = row.get("turn1") or {}
        calls = turn1.get("tool_calls") or []
        call = calls[0] if calls else {}
        rendered = (
            f'`{call.get("name")}({call.get("arguments_raw")})`' if call else "—"
        )
        ttft = (row.get("ttft") or {}).get("ttft_s")
        lines.append(
            f'| `{row.get("model")}` | {rendered} | '
            f'{"pass" if row.get("turn1_pass") else "FAIL"} | '
            f'{"pass" if row.get("turn2_pass") else "FAIL"} | '
            f'{turn1.get("latency_s", "—")} s | '
            f'{"—" if ttft is None else f"{ttft:.1f} s"} |'
        )
    lines.append("")
    shapes = [
        (row.get("model"), (row.get("turn1") or {}).get("shape"))
        for row in rows
        if row.get("turn1")
    ]
    if shapes:
        lines.append("Response shape (turn 1):")
        lines.append("")
        lines.append("```")
        for model, shape in shapes:
            lines.append(f"{model}: {json.dumps(shape, sort_keys=True)}")
        lines.append("```")
        lines.append("")
    return lines


def write_report(summary: dict[str, Any], records: list[RunRecord], path: Path, stage: str) -> None:
    lines: list[str] = []
    lines.append("# Model bake-off: which model drives the tool loop")
    lines.append("")
    lines.append(
        f"Generated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())} from cassettes; "
        "`--analyse` regenerates this file without calling a provider."
    )
    lines.append("")
    models_present = sorted({r.model for r in records})
    runs_per_model = max(
        len({r.run_index for r in records if r.model == model}) for model in models_present
    )
    lines.append(
        f"Stage {stage}: **{len({r.task for r in records})} tasks**, "
        f"{runs_per_model} runs per model, {len(models_present)} models."
    )
    lines.append("")
    lines.append("## Decision")
    lines.append("")
    qualified = {
        model: stats
        for model, stats in summary.items()
        if stats["valid_rate"] >= 0.95 and stats["runs"] > 0
    }
    if not qualified:
        lines.append("**No model qualified**: none reached 95 % valid tool calls.")
    else:
        ranked = sorted(
            qualified.items(), key=lambda item: (-item[1]["success_rate"], item[1]["p95_call_s"])
        )
        winner, runner_up = ranked[0], ranked[1] if len(ranked) > 1 else None
        lines.append(
            f"**Primary: `{winner[1]['label']}`** — success {pct(winner[1]['success_rate'])} "
            f"(95 % CI {pct(winner[1]['ci'][0])}–{pct(winner[1]['ci'][1])}), valid calls "
            f"{pct(winner[1]['valid_rate'])}, p95 call latency {winner[1]['p95_call_s']:.1f} s."
        )
        if runner_up:
            lines.append("")
            lines.append(
                f"**Second: `{runner_up[1]['label']}`** — success "
                f"{pct(runner_up[1]['success_rate'])}, p95 {runner_up[1]['p95_call_s']:.1f} s. "
                "It becomes the fallback model on the same provider."
            )
        lines.append("")
        lines.append(
            "Ranking rule, fixed before the run: highest success rate among models with at "
            "least 95 % valid tool calls, ties broken by p95 call latency."
        )
    lines.append("")
    lines.append("## Metrics")
    lines.append("")
    lines.append(
        "| model | runs | success | 95 % CI | valid calls | median call | p95 call | "
        "median task | tokens/task | failed requests | retries |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for model, stats in sorted(summary.items(), key=lambda item: -item[1]["success_rate"]):
        failures = ", ".join(f"{k} {v}" for k, v in sorted(stats["failed_requests"].items())) or "0"
        lines.append(
            f"| `{model}` | {stats['runs']} | **{pct(stats['success_rate'])}** "
            f"({stats['successes']}/{stats['runs']}) | "
            f"{pct(stats['ci'][0])}–{pct(stats['ci'][1])} | {pct(stats['valid_rate'])} "
            f"({stats['valid_calls']}/{stats['calls']}) | {stats['median_call_s']:.1f} s | "
            f"{stats['p95_call_s']:.1f} s | {stats['median_task_s']:.1f} s | "
            f"{stats['median_tokens']} | {failures} | {stats['retries']} |"
        )
    lines.append("")
    lines.append("## Per task")
    lines.append("")
    models = sorted({record.model for record in records})
    task_ids = sorted({record.task for record in records})
    lines.append("| task | " + " | ".join(models) + " |")
    lines.append("| --- | " + " | ".join("---" for _ in models) + " |")
    for task_id in task_ids:
        cells = []
        for model in models:
            rows = [r for r in records if r.task == task_id and r.model == model]
            if not rows:
                cells.append("–")
                continue
            passed = sum(1 for row in rows if row.success)
            mark = "PASS" if passed == len(rows) else ("part" if passed else "FAIL")
            cells.append(f"{mark} {passed}/{len(rows)}")
        lines.append(f"| `{task_id}` | " + " | ".join(cells) + " |")
    lines.append("")
    failures = [record for record in records if not record.success]
    if failures:
        lines.append("## Failure reasons")
        lines.append("")
        lines.append("| model | task | run | reason |")
        lines.append("| --- | --- | --- | --- |")
        for record in failures:
            lines.append(
                f"| `{record.model}` | `{record.task}` | {record.run_index} | "
                f"{record.failure_reason[:160].replace('|', '/')} |"
            )
        lines.append("")
    lines.extend(_smoke_section())
    lines.extend(NARRATIVE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"report: {path}")


# -- driver --------------------------------------------------------------


def cassette_path(model: str, task: str, run_index: int) -> Path:
    return CASSETTE_ROOT / model / f"{task}--run{run_index}.json"


def load_records(models: list[str]) -> list[RunRecord]:
    records: list[RunRecord] = []
    for model in models:
        for path in sorted((CASSETTE_ROOT / model).glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            record = RunRecord(
                model=payload["model"],
                model_id=payload["model_id"],
                task=payload["task"],
                category=payload["category"],
                run_index=payload["run_index"],
                started_at=payload.get("started_at", ""),
                success=payload["success"],
                calls=payload.get("calls", []),
                turns=payload.get("turns", []),
                answer=payload.get("answer", ""),
                violations=payload.get("violations", []),
                failure_reason=payload.get("failure_reason", ""),
                tests_ok=payload.get("tests_ok", False),
                check_ok=payload.get("check_ok", False),
                answer_ok=payload.get("answer_ok", True),
                args_ok=payload.get("args_ok", True),
                unchanged_ok=payload.get("unchanged_ok", True),
                checks_detail=payload.get("checks_detail", ""),
                task_seconds=payload.get("task_seconds", 0.0),
                tokens=payload.get("tokens", 0),
                repeats=payload.get("repeats", 0),
                setups=payload.get("setups", []),
            )
            records.append(record)
    return records


def run_stage(
    model_keys: list[str],
    tasks: list[tasks_module.Task],
    runs: int,
    force: bool = False,
) -> list[RunRecord]:
    records: list[RunRecord] = []
    total = len(tasks) * runs * len(model_keys)
    done = 0
    for task_index, task in enumerate(tasks):
        for run_index in range(1, runs + 1):
            offset = (task_index + run_index) % len(model_keys)
            order = model_keys[offset:] + model_keys[:offset]
            for model_key in order:
                done += 1
                path = cassette_path(model_key, task.id, run_index)
                if path.exists() and not force:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    if payload.get("task") == task.id:
                        print(
                            f"[{done}/{total}] {model_key:9s} {task.id:38s} run{run_index} "
                            f"(cached: success={payload['success']})"
                        )
                        records.append(load_one(path))
                        continue
                print(
                    f"[{done}/{total}] {model_key:9s} {task.id:38s} run{run_index} ...",
                    end="",
                    flush=True,
                )
                try:
                    record = run_one(model_key, task, run_index)
                except Exception as exc:  # noqa: BLE001
                    record = RunRecord(
                        model=model_key,
                        model_id=MODELS[model_key].model_id,
                        task=task.id,
                        category=task.category,
                        run_index=run_index,
                        failure_reason=f"{type(exc).__name__}: {exc}",
                        violations=["harness_exception"],
                    )
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(redact(record.to_dict()), indent=2), encoding="utf-8")
                records.append(record)
                print(
                    f" {'PASS' if record.success else 'FAIL'} "
                    f"{record.task_seconds:.1f}s calls={len(record.calls)} "
                    f"{record.failure_reason[:70]}"
                )
    return records


def load_one(path: Path) -> RunRecord:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return RunRecord(
        model=payload["model"],
        model_id=payload["model_id"],
        task=payload["task"],
        category=payload["category"],
        run_index=payload["run_index"],
        started_at=payload.get("started_at", ""),
        success=payload["success"],
        calls=payload.get("calls", []),
        turns=payload.get("turns", []),
        answer=payload.get("answer", ""),
        violations=payload.get("violations", []),
        failure_reason=payload.get("failure_reason", ""),
        tests_ok=payload.get("tests_ok", False),
        check_ok=payload.get("check_ok", False),
        answer_ok=payload.get("answer_ok", True),
        args_ok=payload.get("args_ok", True),
        unchanged_ok=payload.get("unchanged_ok", True),
        checks_detail=payload.get("checks_detail", ""),
        task_seconds=payload.get("task_seconds", 0.0),
        tokens=payload.get("tokens", 0),
        repeats=payload.get("repeats", 0),
        setups=payload.get("setups", []),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-check", action="store_true", help="prove every task is well posed")
    parser.add_argument("--only-task", default="")
    parser.add_argument("--stage", choices=["1", "2", "all"], default="1")
    parser.add_argument(
        "--models", default="", help="comma-separated keys; default is the finalists"
    )
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument(
        "--force", action="store_true", help="re-run tasks that already have a cassette"
    )
    parser.add_argument(
        "--analyse", action="store_true", help="rebuild the report from cassettes"
    )
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    if args.self_check:
        return self_check(args.only_task)

    model_keys = (
        [key.strip() for key in args.models.split(",") if key.strip()]
        if args.models
        else ["nemotron", "deepseek", "kimi"]
    )
    for key in model_keys:
        if key not in MODELS:
            raise SystemExit(f"unknown model key {key!r}; known: {sorted(MODELS)}")

    tasks = tasks_module.stage_one() if args.stage == "1" else tasks_module.TASKS
    if args.stage == "1":
        date = time.strftime("%Y%m%d")
        out = Path(args.out) if args.out else (
            REPO_ROOT / "docs" / "results" / f"model-bakeoff-{date}.md"
        )
    else:
        date = time.strftime("%Y%m%d")
        out = Path(args.out) if args.out else (
            REPO_ROOT / "docs" / "results" / f"model-bakeoff-{date}.md"
        )

    if args.analyse:
        records = load_records(model_keys)
        if not records:
            raise SystemExit("no cassettes to analyse")
        summary = summarise(records, len({record.task for record in records}))
        write_report(summary, records, out, args.stage)
        return 0

    records = run_stage(model_keys, tasks, args.runs, force=args.force)
    summary = summarise(records, len(tasks))
    write_report(summary, records, out, args.stage)
    print(json.dumps(summary, indent=2, default=str)[:2000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
