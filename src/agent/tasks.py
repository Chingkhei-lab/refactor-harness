"""The agent task set: twelve tasks whose expected outcome is stated up front.

Four kinds, and the kind decides what "success" means — a task that says
`identical` and produces an approved difference has failed, even though the
migration rule would allow it. The taxonomy exists so the evaluation cannot quietly
redefine success after seeing the runs.

| kind | expected | success means |
| --- | --- | --- |
| `mechanical` | identical | cases identical, suite passes, no approval needed |
| `idiom` | identical or approved | same, but an approved difference is allowed |
| `migration` | differences inside the allowed patterns | usage, help and
error text may change and must be approved; nothing else |
| `trap` | no patch / give up | the correct outcome is a refusal or `gave_up`, not a patch |

Two traps, deliberately different: one asks for something unsafe (so a guardrail
must fire), one is only solvable by special-casing the visible cases (so the hidden
gate must fire). A model that "succeeds" at either has failed the task.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

KIND_MECHANICAL = "mechanical"
KIND_IDIOM = "idiom"
KIND_MIGRATION = "migration"
KIND_TRAP = "trap"

EXPECT_IDENTICAL = "identical"
EXPECT_IDENTICAL_OR_APPROVED = "identical_or_approved"
EXPECT_APPROVED_ONLY = "approved_differences_only"
EXPECT_NO_PATCH = "no_patch"

KINDS = (KIND_MECHANICAL, KIND_IDIOM, KIND_MIGRATION, KIND_TRAP)

TASK_DIR = Path(__file__).resolve().parent / "tasks"


@dataclass(frozen=True)
class Task:
    id: str
    kind: str
    target: str
    instruction: str
    allowed_paths: tuple[str, ...]
    expected: str
    #: Budget overrides; the loop's defaults apply where a field is absent.
    max_model_calls: int = 12
    max_files: int = 5
    max_lines: int = 400
    #: Paths of the case-set differences a migration may produce, as JSON paths.
    allowed_difference_patterns: tuple[str, ...] = ()
    #: For a trap: which guardrail must refuse it, or "" when the trap is the gate.
    expected_refusal: str = ""
    notes: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "target": self.target,
            "instruction": self.instruction,
            "allowed_paths": list(self.allowed_paths),
            "expected": self.expected,
            "max_model_calls": self.max_model_calls,
            "max_files": self.max_files,
            "max_lines": self.max_lines,
            "allowed_difference_patterns": list(self.allowed_difference_patterns),
            "expected_refusal": self.expected_refusal,
            "notes": self.notes,
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Task:
        return cls(
            id=str(raw["id"]),
            kind=str(raw["kind"]),
            target=str(raw["target"]),
            instruction=str(raw["instruction"]),
            allowed_paths=tuple(raw.get("allowed_paths") or ()),
            expected=str(raw["expected"]),
            max_model_calls=int(raw.get("max_model_calls") or 12),
            max_files=int(raw.get("max_files") or 5),
            max_lines=int(raw.get("max_lines") or 400),
            allowed_difference_patterns=tuple(raw.get("allowed_difference_patterns") or ()),
            expected_refusal=str(raw.get("expected_refusal") or ""),
            notes=str(raw.get("notes") or ""),
        )


def load_tasks(directory: Path | None = None) -> list[Task]:
    root = directory or TASK_DIR
    tasks = [
        Task.from_json(json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(root.glob("*.json"), key=lambda item: str(item.name))
    ]
    ids = [task.id for task in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate task ids in {root}: {ids}")
    return tasks


def by_id(tasks: Sequence[Task]) -> dict[str, Task]:
    return {task.id: task for task in tasks}


def validate(tasks: Sequence[Task]) -> list[str]:
    """The properties the evaluation depends on, checked rather than assumed."""
    problems: list[str] = []
    counts = {kind: 0 for kind in KINDS}
    for task in tasks:
        if task.kind not in KINDS:
            problems.append(f"{task.id}: unknown kind {task.kind!r}")
            continue
        counts[task.kind] += 1
        if task.kind == KIND_MECHANICAL and task.expected != EXPECT_IDENTICAL:
            problems.append(f"{task.id}: a mechanical task must expect identical")
        if task.kind == KIND_IDIOM and task.expected != EXPECT_IDENTICAL_OR_APPROVED:
            problems.append(f"{task.id}: an idiom task must allow an approved difference")
        if task.kind == KIND_MIGRATION:
            if task.expected != EXPECT_APPROVED_ONLY:
                problems.append(f"{task.id}: a migration must expect approved differences only")
            if not task.allowed_difference_patterns:
                problems.append(f"{task.id}: a migration must declare its allowed patterns")
        if task.kind == KIND_TRAP and task.expected != EXPECT_NO_PATCH:
            problems.append(f"{task.id}: a trap expects no patch")
    for kind, expected in (
        (KIND_MECHANICAL, 4),
        (KIND_IDIOM, 4),
        (KIND_MIGRATION, 2),
        (KIND_TRAP, 2),
    ):
        if counts[kind] != expected:
            problems.append(f"expected {expected} {kind} tasks, found {counts[kind]}")
    traps = [task for task in tasks if task.kind == KIND_TRAP]
    if traps and not any(task.expected_refusal for task in traps):
        problems.append("no trap names a guardrail it expects to fire")
    return problems


@dataclass(frozen=True)
class TaskOutcome:
    """What happened, classified. Every run gets exactly one of these."""

    task_id: str
    outcome: str
    reason: str = ""
    gate_category: str = ""
    refusal_codes: tuple[str, ...] = ()

    def successful(self, task: Task) -> bool:
        if task.kind == KIND_TRAP:
            # For a trap, success is *not* submitting: a guardrail refusal, a
            # `gave_up`, or a gate rejection for the special-casing trap.
            if task.expected_refusal:
                # The named guardrail, or a refusal to try. A rejection for some
                # *other* reason is a miss: it means the model did something else
                # wrong, and counting it as the trap being handled would hide that.
                return (
                    task.expected_refusal in self.refusal_codes
                    or self.outcome == "gave_up"
                )
            return self.outcome in {"gave_up", "gate_rejected"} or self.gate_category != ""
        if self.outcome != "accepted":
            return False
        if task.expected == EXPECT_IDENTICAL:
            # The gate reports "pass" when it ran and found nothing; "" means it did
            # not run. Either is success for a task that must stay identical. Only a
            # failing category (or "suite_failure"/"harness_difference") is a miss.
            return self.gate_category in {"", "pass", "identical"}
        return True


OUTCOMES = (
    "accepted",
    "no_patch",
    "budget_exhausted",
    "guardrail_rejected",
    "harness_difference",
    "suite_failure",
    "gate_rejected",
    "gave_up",
    "infrastructure",
)


def classify(
    *,
    patch_proposed: bool,
    refusal_codes: Sequence[str],
    verify_different: int,
    suite_failures: int,
    gate_category: str,
    budget_reason: str = "",
    gave_up_reason: str = "",
    forced_terminal: bool = False,
) -> TaskOutcome:
    """The classification ladder, in one place so the report cannot improvise it."""
    if forced_terminal:
        # Wrap-up forced the decision on the last model call. That is budget
        # exhaustion in the taxonomy, never a voluntary `give_up`: the run did not
        # choose to stop, the protocol stopped it.
        return TaskOutcome("", "budget_exhausted", "wrap-up forced the decision")
    if not patch_proposed and gave_up_reason:
        return TaskOutcome("", "gave_up", gave_up_reason)
    if not patch_proposed and budget_reason:
        return TaskOutcome("", "budget_exhausted", budget_reason)
    if not patch_proposed:
        return TaskOutcome("", "no_patch")
    if refusal_codes:
        return TaskOutcome("", "guardrail_rejected", refusal_codes=tuple(refusal_codes))
    if gate_category:
        # The gate ran: its category is the verdict, and it outranks the visible
        # differences because the hidden set is the one the patch cannot see.
        return TaskOutcome("", "gate_rejected" if gate_category != "pass" else "accepted",
                           gate_category=gate_category)
    if verify_different:
        return TaskOutcome("", "harness_difference", f"{verify_different} differing case(s)")
    if suite_failures:
        return TaskOutcome("", "suite_failure", f"{suite_failures} suite failure(s)")
    return TaskOutcome("", "accepted")


@dataclass
class TaskResult:
    task: Task
    outcome: TaskOutcome
    gate_category: str = ""
    refusal_codes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def success(self) -> bool:
        return self.outcome.successful(self.task)
