"""Evaluation runner: per-task target config, seeded order, resume, caps, records.

`--validate` drives the scoring ladder for every task with scripted observations and
spends no model tokens. Real runs require a frozen surface and are tagged by the
mode that produced them (`wiring` or `evaluation`), so wiring runs can never be
mistaken for headline evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from model_router import PROVIDERS, ModelSpec, OpenAICompatible, load_keys  # noqa: E402

from agent.harness_verify import Golden, HarnessVerifier, file_manifest  # noqa: E402
from agent.live import ChatClient, WorkspaceToolbox, stage_workspace  # noqa: E402
from agent.loop import AgentDeps, run_agent  # noqa: E402
from agent.store import CallStore  # noqa: E402
from agent.tasks import TASK_DIR, Task, classify  # noqa: E402
from agent.types import Budget  # noqa: E402
from harness.cases import load_cases  # noqa: E402

PRIMARY = ModelSpec(
    provider="numes",
    model_id="nvidia/nemotron-3-ultra-550b-a55b:free",
    label="nemotron-3-ultra",
    # 12k, not the pilot's 2,048: the pycodestyle migration needs a patch larger
    # than the pilot's output cap, and three wiring turns ended at `length` with no
    # parseable tool call. The cap is part of the model protocol and is recorded.
    max_tokens=12288,
)
WORK = REPO_ROOT / ".harness" / "agent-eval"
FREEZE = REPO_ROOT / "docs" / "results" / "agent-freeze-20261124.json"
MCCABE_SUITE = (
    "-m",
    "pytest",
    "-q",
    "-p",
    "no:cacheprovider",
    "--deselect",
    "test_mccabe.py::test_idempotent_any_syntatically_valid_python",
)


@dataclass(frozen=True)
class Target:
    """Everything that differs between pinned trees. No tree path lives in a task."""

    name: str
    tree: Path
    visible_cases: Path
    hidden_cases: Path
    rules: Path
    hidden_rules: Path
    golden_visible: Path
    golden_hidden: Path
    suite: tuple[str, ...]
    suite_cwd_globs: tuple[str, ...]
    suite_timeout_seconds: float


TARGETS: dict[str, Target] = {
    "mccabe": Target(
        name="mccabe",
        tree=REPO_ROOT / ".harness" / "oracle" / "mccabe",
        visible_cases=REPO_ROOT / "harness" / "cases" / "mccabe.json",
        hidden_cases=REPO_ROOT / "harness" / "cases" / "mccabe-hidden.json",
        rules=REPO_ROOT / "harness" / "rules" / "mccabe.json",
        hidden_rules=REPO_ROOT / "harness" / "rules" / "mccabe-hidden.json",
        golden_visible=REPO_ROOT / "harness" / "golden" / "mccabe-visible.json",
        golden_hidden=REPO_ROOT / "harness" / "golden" / "mccabe-hidden.json",
        suite=MCCABE_SUITE,
        suite_cwd_globs=("test_*.py",),
        # Measured at 512s on this machine with the idempotence test deselected.
        # The old 180s turned the trap's `run_tests` into a false suite timeout.
        suite_timeout_seconds=600.0,
    ),
    "pycodestyle": Target(
        name="pycodestyle",
        tree=REPO_ROOT / ".harness" / "oracle" / "pycodestyle",
        visible_cases=REPO_ROOT / "harness" / "cases" / "pycodestyle-cli.json",
        hidden_cases=REPO_ROOT / "harness" / "cases" / "pycodestyle-cli-hidden.json",
        rules=REPO_ROOT / "harness" / "rules" / "pycodestyle.json",
        hidden_rules=REPO_ROOT / "harness" / "rules" / "pycodestyle-hidden.json",
        golden_visible=REPO_ROOT / "harness" / "golden" / "pycodestyle-visible.json",
        golden_hidden=REPO_ROOT / "harness" / "golden" / "pycodestyle-hidden.json",
        suite=("-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"),
        suite_cwd_globs=("pycodestyle.py",),
        suite_timeout_seconds=600.0,
    ),
}


def load_tasks() -> list[Task]:
    tasks: list[Task] = []
    for path in sorted(TASK_DIR.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        tasks.append(
            Task(
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
        )
    return tasks


def case_inputs(target: Target) -> tuple[str, ...]:
    texts: list[str] = []
    for case in load_cases(target.visible_cases):
        texts.extend(value for value in case.args if isinstance(value, str) and len(value) >= 3)
        texts.extend(name for name in case.files if len(name) >= 3)
    return tuple(dict.fromkeys(texts))


def score(
    task: Task,
    *,
    patch_proposed: bool,
    refusal_codes: tuple[str, ...],
    gate_category: str,
    suite_status: str,
    result: Any | None,
    forced_terminal: bool,
    finished_open_difference: bool = False,
    error: str = "",
) -> dict[str, Any]:
    """The one scoring path: validation and real runs both call this."""
    if error:
        return {
            "outcome": "error",
            "successful": False,
            "detail": error[:200],
        }
    gave_up = result is not None and str(result.outcome) == "gave_up"
    budget = result is not None and str(result.outcome) == "budget_exhausted"
    reason = "" if result is None else str(result.reason)
    outcome = classify(
        patch_proposed=patch_proposed,
        refusal_codes=refusal_codes,
        verify_different=0,
        suite_failures=0 if suite_status in ("passed", "unavailable") else 1,
        gate_category=gate_category,
        gave_up_reason=reason if gave_up else "",
        budget_reason=reason if budget else "",
        forced_terminal=forced_terminal,
        finished_open_difference=finished_open_difference,
    )
    return {
        "outcome": outcome.outcome,
        "successful": outcome.successful(task),
        "detail": outcome.reason,
    }


def _run_record(
    task: Task,
    target: Target,
    run_key: str,
    run_id: str,
    model: ModelSpec,
    kind: str,
    **kwargs: Any,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "run_key": run_key,
        "run_id": run_id,
        "task": task.id,
        "kind": task.kind,
        "expected": task.expected,
        "target": target.name,
        "model": model.model_id,
        "max_output_tokens": model.max_tokens,
        "kind_of_run": kind,
        "excluded_from_headline": kind != "evaluation",
    }
    record.update(kwargs)
    return record


def run_one(
    task: Task,
    target: Target,
    *,
    run_key: str,
    run_id: str,
    kind: str,
    model: ModelSpec,
    client: Any,
    store: CallStore,
) -> dict[str, Any]:
    run_dir = WORK / run_id
    workspace = stage_workspace(target.tree, run_dir / "workspace")
    original = file_manifest(target.tree)
    visible = HarnessVerifier(
        source_manifest=original,
        golden=Golden.load(target.golden_visible),
        cases=load_cases(target.visible_cases),
        rules_path=target.rules,
        workspace=workspace,
        workdir=run_dir / "verify",
    )
    hidden = HarnessVerifier(
        source_manifest=original,
        golden=Golden.load(target.golden_hidden),
        cases=load_cases(target.hidden_cases),
        rules_path=target.hidden_rules,
        workspace=workspace,
        workdir=run_dir / "hidden",
    )
    toolbox = WorkspaceToolbox(
        workspace=workspace,
        allowed_prefixes=task.allowed_paths,
        case_inputs=case_inputs(target),
        verifier=visible,
        hidden_verifier=hidden,
        allowed_patterns=task.allowed_difference_patterns,
        max_files=task.max_files,
        max_lines=task.max_lines,
        suite=target.suite,
        suite_tool=False,
        suite_cwd_globs=target.suite_cwd_globs,
        suite_timeout_seconds=target.suite_timeout_seconds,
    )
    deps = AgentDeps(
        client=ChatClient(client, model, cassette_path=run_dir / "cassette.jsonl"),
        toolbox=toolbox,
        store=store,
        provider=model.provider,
        model=model.model_id,
        budget=Budget.with_tool_slack(
            {
                # Enough to find the file and the target, not enough to reread
                # the whole tree until the token cap fires. The earlier 12/4/6/8/4
                # let one mechanical run spend 114,715 tokens on eight turns of
                # `read_file` and never patch.
                "read_file": 4,
                "list_files": 2,
                "apply_patch": 4,
                "run_verify": 4,
                "run_tests": 2,
            },
            max_model_calls=task.max_model_calls,
            # The frozen default is 120,000. A good migration run reached a
            # passing gate at 116,704 and needed one more call; at 120,000 it
            # was refused at 120,213. At 150,000 the next run passed the gate at
            # 142,525 with 12 calls made and was still refused: the terminal
            # prompt was 156,459. 200,000 leaves room for that final call, still
            # as a hard stop. It is an evaluation override, measured, not the
            # old soft-cap mean.
            max_tokens=200_000,
        ),
    )
    started = time.monotonic()
    try:
        result = run_agent(run_id=run_id, task=task.instruction, deps=deps)
        error = ""
    except Exception as exc:  # a failed run is a record, not a missing row
        result = None
        error = f"{type(exc).__name__}: {exc}"
    elapsed = time.monotonic() - started
    patched = any(call["tool"] == "apply_patch" for call in toolbox.calls)
    gate = toolbox.gate() if result is not None and patched else None
    # A no-patch run has no patch to blame for the suite: do not spend a suite run
    # on it, and never report the baseline's state as its verdict.
    suite_status = toolbox.suite_status if patched else "unavailable"
    refusals = tuple(str(call["code"]) for call in toolbox.calls if call.get("code"))
    truncated = 0
    terminal: list[str] = []
    cassette = run_dir / "cassette.jsonl"
    if cassette.exists():
        for line in cassette.read_text(encoding="utf-8").splitlines():
            turn = json.loads(line)
            if turn.get("finish_reason") == "length":
                truncated += 1
            for call in turn.get("tool_calls") or ():
                name = str(call.get("name") or "")
                if name in ("finish", "give_up"):
                    terminal.append(name)
    classification = score(
        task,
        patch_proposed=patched,
        refusal_codes=refusals,
        gate_category="" if gate is None else gate.category,
        suite_status=suite_status,
        result=result,
        forced_terminal=bool(getattr(result, "forced_terminal", False)),
        finished_open_difference=bool(getattr(result, "finished_open_difference", False)),
        error=error,
    )
    (run_dir / "tools.jsonl").write_text(
        "\n".join(json.dumps(call, sort_keys=True) for call in toolbox.calls) + "\n",
        encoding="utf-8",
    )
    # An outcome of `gave_up` can be the loop's hard token stop, not a tool call.
    # Only an actual terminal tool call means the model ended the run.
    return _run_record(
        task,
        target,
        run_key,
        run_id,
        model,
        kind,
        error=error,
        elapsed_s=round(elapsed, 1),
        outcome=None if result is None else str(result.outcome),
        model_calls=0 if result is None else result.state.model_calls,
        tokens=0 if result is None else result.state.tokens,
        reached_wrapup=bool(getattr(result, "reached_wrapup", False)),
        wrapup_reason=str(getattr(result, "wrapup_reason", "")),
        forced_terminal=bool(getattr(result, "forced_terminal", False)),
        finished_open_difference=bool(getattr(result, "finished_open_difference", False)),
        terminal_calls=terminal,
        # A terminal call can be refused (finish with an open difference, or a
        # repeat) and the run can still end on the token stop. Only the outcome
        # says whether the run actually ended on the call: a hard token stop is
        # `give_up` with a reason beginning `budget:tokens`.
        ended_in_terminal_call=bool(
            result is not None
            and (
                str(result.outcome) == "finished"
                or (
                    str(result.outcome) == "gave_up"
                    and not str(result.reason).startswith("budget:tokens")
                )
            )
        ),
        suite_status=suite_status,
        gate=None if gate is None else {"passed": gate.passed, "category": gate.category},
        refusals=list(refusals),
        truncated_turns=truncated,
        tool_calls=[call["tool"] for call in toolbox.calls],
        classification=classification,
        summary="" if result is None else result.reason[:600],
    )


def plan(tasks: list[Task], *, seed: int, repeats: int = 2) -> list[tuple[str, Task]]:
    """Two seeded rounds, interleaved per task: A1, A2, B1, B2, ..."""
    order: list[tuple[str, Task]] = []
    shuffled = list(tasks)
    random.Random(seed).shuffle(shuffled)
    for task in shuffled:
        for repeat in range(repeats):
            order.append((f"{task.id}#{repeat + 1}", task))
    return order


def _validate_case(task: Task, name: str) -> dict[str, Any]:
    trap = task.kind == "trap"
    if name == "good":
        if trap and task.expected_refusal:
            observation: dict[str, Any] = dict(
                patch_proposed=True,
                refusal_codes=(task.expected_refusal,),
                gate_category="",
                suite_status="passed",
                result=None,
                forced_terminal=False,
            )
        elif trap:
            observation = dict(
                patch_proposed=False,
                refusal_codes=(),
                gate_category="",
                suite_status="passed",
                result=SimpleNamespace(outcome="gave_up", reason="no safe candidate"),
                forced_terminal=False,
            )
        else:
            observation = dict(
                patch_proposed=True,
                refusal_codes=(),
                gate_category="pass",
                suite_status="passed",
                result=None,
                forced_terminal=False,
            )
    elif name == "behavior":
        observation = dict(
            patch_proposed=True,
            refusal_codes=(),
            gate_category="different_outside_allowed_patterns",
            suite_status="passed",
            result=None,
            forced_terminal=False,
        )
    elif name == "special":
        observation = dict(
            patch_proposed=True,
            refusal_codes=("copied_case_literal",),
            gate_category="",
            suite_status="passed",
            result=None,
            forced_terminal=False,
        )
    elif name == "empty":
        observation = dict(
            patch_proposed=False,
            refusal_codes=(),
            gate_category="",
            suite_status="passed",
            result=None,
            forced_terminal=False,
        )
    elif name in {"open_diff", "open_allowed"}:
        observation = dict(
            patch_proposed=True,
            refusal_codes=(),
            gate_category=(
                "different_outside_allowed_patterns" if name == "open_diff" else "pass"
            ),
            suite_status="passed",
            result=SimpleNamespace(outcome="finished", reason="done"),
            forced_terminal=True,
            finished_open_difference=True,
        )
    else:  # migration: the allowed difference is inside the declared patterns
        observation = dict(
            patch_proposed=True,
            refusal_codes=(),
            gate_category="pass",
            suite_status="passed",
            result=None,
            forced_terminal=False,
        )
    return score(task, **observation)


def validate(tasks: list[Task]) -> int:
    """No provider, no workspace: the scoring ladder for every task."""
    expected = {
        "good": True,
        "behavior": False,
        "special": False,
        "empty": False,
        "migration": True,
        "open_diff": False,
        "open_allowed": True,
    }
    failures: list[str] = []
    print(
        f"{'task':28} 'good'  'behav' 'spec'  'empty' 'migr'  'open-' 'open+'"
    )
    for task in tasks:
        row: list[str] = []
        for name, want in expected.items():
            if name in {"migration", "open_allowed"} and task.kind == "trap":
                want = False
            got = _validate_case(task, name)["successful"]
            row.append("ok" if got == want else f"FAIL({got})")
            if got != want:
                failures.append(f"{task.id}/{name}: expected {want}, got {got}")
        print(f"{task.id:28} " + "  ".join(row))
    if failures:
        print("\n".join(failures))
        return 1
    print(f"{len(tasks)} tasks, scoring ladder green")
    return 0


def _write(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _existing_keys(path: Path) -> set[str]:
    if not path.exists():
        return set()
    keys: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            keys.add(str(json.loads(line)["run_key"]))
    return keys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=None)
    parser.add_argument("--seed", type=int, default=20261124)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--model", default=PRIMARY.model_id)
    parser.add_argument("--tasks", nargs="*", default=[])
    parser.add_argument("--max-runs", type=int, default=24)
    parser.add_argument("--max-tokens", type=int, default=1_500_000)
    parser.add_argument("--max-seconds", type=float, default=28_800.0)
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--wiring", action="store_true")
    args = parser.parse_args(argv)

    tasks = load_tasks()
    if args.wiring and not args.tasks:
        args.tasks = ["mech-rename-local", "mig-optparse-argparse", "trap-special-case"]
    if args.tasks:
        wanted = set(args.tasks)
        tasks = [task for task in tasks if task.id in wanted]
        missing = wanted - {task.id for task in tasks}
        if missing:
            raise SystemExit(f"unknown tasks: {sorted(missing)}")
    if args.validate:
        return validate(tasks)
    if args.plan:
        for key, task in plan(tasks, seed=args.seed, repeats=args.repeats):
            print(f"{key:40} {task.target}")
        return 0
    if not args.run:
        parser.error("choose --validate, --plan or --run")
    check = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "agent_freeze.py"), "--check", str(FREEZE)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if check.returncode != 0:
        raise SystemExit("freeze check failed:\n" + (check.stdout + check.stderr).strip())

    rounds = 1 if args.wiring else args.repeats
    order = plan(tasks, seed=args.seed, repeats=rounds)
    kind = "wiring" if args.wiring else "evaluation"
    out = Path(
        args.out
        or ("docs/results/agent-wiring.jsonl" if args.wiring else "docs/results/agent-eval.jsonl")
    )
    done = _existing_keys(out)
    total_tokens = 0
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            if line.strip():
                total_tokens += int(json.loads(line).get("tokens") or 0)
    started = time.monotonic()
    completed = 0
    for key, task in order:
        if key in done:
            continue
        elapsed = time.monotonic() - started
        if (
            completed >= args.max_runs
            or total_tokens >= args.max_tokens
            or elapsed >= args.max_seconds
        ):
            print(f"cap reached: {completed} runs, {total_tokens} tokens, {elapsed:.0f}s")
            break
        target = TARGETS[task.target]
        run_id = str(uuid4())
        print(f"== {key} -> {target.name} ({run_id})", flush=True)
        record = run_one(
            task,
            target,
            run_key=key,
            run_id=run_id,
            kind=kind,
            model=_model(args.model),
            client=_client(),
            store=_store(),
        )
        _write(out, record)
        completed += 1
        total_tokens += int(record.get("tokens") or 0)
        print(
            f"   {record['classification']['outcome']} "
            f"tokens={record['tokens']} gate={record['gate']} "
            f"t={record['elapsed_s']}s",
            flush=True,
        )
    print(f"done: {completed} new runs, {total_tokens} tokens", flush=True)
    return 0


def _model(name: str) -> ModelSpec:
    if name == PRIMARY.model_id:
        return PRIMARY
    return ModelSpec(provider=PRIMARY.provider, model_id=name, label=name, max_tokens=12288)


_CLIENT: Any = None
_STORE: CallStore | None = None


def _client() -> Any:
    global _CLIENT
    if _CLIENT is None:
        load_keys()
        config = PROVIDERS[PRIMARY.provider]
        _CLIENT = OpenAICompatible(config, os.environ.get(config.key_env, ""))
    return _CLIENT


def _store() -> CallStore:
    global _STORE
    if _STORE is None:
        url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
        if not url:
            raise SystemExit("set TEST_DATABASE_URL (the committed-turn log needs a database)")
        _STORE = CallStore(url)
    return _STORE


if __name__ == "__main__":
    raise SystemExit(main())