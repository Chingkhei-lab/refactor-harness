"""`run_verify` and the hidden gate (Checkpoint 14, design §6).

The model must be able to run the harness, and must not be able to *read the answer
off it*. Three limits make that true, and all three are enforced here rather than
asked for in the prompt:

* **A verify result is counts plus at most five differing cases**, each truncated
  to 500 characters per side. A model handed the whole diff optimises the diff.
* **The verify budget is 8 calls per run.** Otherwise "run it again until green" is
  a search over the visible set, which is exactly the special-casing the hidden set
  exists to catch.
* **`finish` may be attempted twice, and answers pass/fail with a category.** A
  third submission is refused without running anything, because each attempt is a
  query against the hidden set: with unlimited attempts, a model can binary-search
  it by resubmitting.

The hidden set itself never enters the agent's workspace: `HiddenGate` holds case
paths from outside the workspace and runs them in a sandbox of its own, after the
run has sealed its candidate.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

MAX_VERIFY_CALLS = 8
MAX_FINISH_ATTEMPTS = 2
MAX_DIFFERING_CASES = 5
MAX_DIFF_CHARS = 500

#: Why a gate attempt failed. Categories, never case details.
GATE_PASS = "pass"
GATE_DIFFERENT_OUTSIDE_PATTERNS = "different_outside_allowed_patterns"
GATE_SUITE_FAILURE = "suite_failure"
GATE_CASE_TIMEOUT = "case_timeout"
GATE_INFRASTRUCTURE = "infrastructure"

#: Where a difference is expected for a migration task and therefore needs a human
#: approval rather than refusing the patch outright (design §7).
DEFAULT_APPROVAL_PATTERNS: tuple[str, ...] = (
    "$.value.stdout",
    "$.value.stderr",
    "$.value.exit",
)


class VerifyBudgetExhausted(RuntimeError):
    def __init__(self, used: int, cap: int) -> None:
        super().__init__(f"run_verify used {used} of {cap} calls")
        self.used = used
        self.cap = cap


class FinishAttemptsExhausted(RuntimeError):
    def __init__(self, attempts: int, cap: int) -> None:
        super().__init__(f"finish attempted {attempts} times; the cap is {cap}")
        self.attempts = attempts
        self.cap = cap


@dataclass(frozen=True)
class CaseDifference:
    """One differing case, small enough to be a hint and not an answer."""

    case_id: str
    entry: str
    path: str
    left: str
    right: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "entry": self.entry,
            "path": self.path,
            "left": truncate_diff(self.left),
            "right": truncate_diff(self.right),
        }


def truncate_diff(text: str) -> str:
    """500 characters, with the count of what was cut.

    Head only, unlike tool output: a difference is a leaf value, and the useful part
    of a leaf is where it starts to differ.
    """
    if len(text) <= MAX_DIFF_CHARS:
        return text
    dropped = len(text) - MAX_DIFF_CHARS
    return text[:MAX_DIFF_CHARS] + f"…[truncated: {dropped} chars]"


@dataclass
class VerifyResult:
    """What `run_verify` hands back: counts, and a bounded sample of differences."""

    identical: int
    different: int
    approved: int
    unsupported: int
    differences: tuple[CaseDifference, ...] = ()
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.different == 0

    def render(self) -> str:
        lines = [
            f"identical={self.identical} different={self.different} "
            f"approved={self.approved} unsupported={self.unsupported}",
        ]
        if self.differences:
            lines.append(
                f"showing {len(self.differences)} of {self.different} differing cases"
                + (" (truncated)" if self.truncated else "")
            )
            for difference in self.differences:
                shown = difference.as_dict()
                lines.append(
                    f"- {shown['case_id']} [{shown['entry']}] at {shown['path']}\n"
                    f"  left : {shown['left']}\n"
                    f"  right: {shown['right']}"
                )
        return "\n".join(lines)


@dataclass(frozen=True)
class GateResult:
    """The hidden gate's verdict: pass or fail, a category, and nothing else."""

    passed: bool
    category: str
    reason: str = ""

    def render(self) -> str:
        # Deliberately small: no case ids, no diffs, nothing to iterate against.
        verb = "pass" if self.passed else "fail"
        return f"hidden gate: {verb} ({self.category}){': ' + self.reason if self.reason else ''}"


class Verifier(Protocol):
    """Whatever can compare two trees and report classes and differences."""

    def run(self, *, differences: int = MAX_DIFFERING_CASES) -> VerifyResult: ...


class HiddenGate(Protocol):
    def evaluate(self, *, allowed_patterns: Sequence[str]) -> GateResult: ...


@dataclass
class VerifySession:
    """Per-run budgets for the two tools that can be used to game the gate."""

    verifier: Verifier | None = None
    gate: HiddenGate | None = None
    verify_calls: int = 0
    finish_attempts: int = 0
    max_verify_calls: int = MAX_VERIFY_CALLS
    max_finish_attempts: int = MAX_FINISH_ATTEMPTS
    history: list[dict[str, Any]] = field(default_factory=list)

    def run_verify(self, *, allowed_patterns: Sequence[str] = ()) -> VerifyResult:
        if self.verify_calls >= self.max_verify_calls:
            raise VerifyBudgetExhausted(self.verify_calls, self.max_verify_calls)
        if self.verifier is None:
            raise RuntimeError("no verifier configured for this run")
        self.verify_calls += 1
        result = self.verifier.run(differences=MAX_DIFFERING_CASES)
        self.history.append(
            {
                "tool": "run_verify",
                "call": self.verify_calls,
                "different": result.different,
                "identical": result.identical,
                "shown": len(result.differences),
            }
        )
        return result

    def finish(self, *, allowed_patterns: Sequence[str] = DEFAULT_APPROVAL_PATTERNS) -> GateResult:
        if self.finish_attempts >= self.max_finish_attempts:
            raise FinishAttemptsExhausted(self.finish_attempts, self.max_finish_attempts)
        if self.gate is None:
            raise RuntimeError("no hidden gate configured for this run")
        self.finish_attempts += 1
        result = self.gate.evaluate(allowed_patterns=allowed_patterns)
        self.history.append(
            {
                "tool": "finish",
                "attempt": self.finish_attempts,
                "passed": result.passed,
                "category": result.category,
                # No case details are recorded either: the record is reproducible
                # from the gate's own artifacts, and a stored detail is a detail
                # that could be shown to a model by accident later.
            }
        )
        return result


def classify_gate(
    *,
    differences_outside_patterns: int,
    suite_failures: int,
    timeouts: int,
    infrastructure_errors: int,
    allowed_patterns: Sequence[str] = DEFAULT_APPROVAL_PATTERNS,
) -> GateResult:
    """The gate's decision table, in one place so it can be tested without a run.

    Order is intentional: infrastructure first (a broken run is not a verdict),
    then non-termination, then differences outside the allowed patterns (the thing
    the agent could hide), then the suite.
    """
    if infrastructure_errors:
        return GateResult(
            False, GATE_INFRASTRUCTURE, f"{infrastructure_errors} infrastructure error(s)"
        )
    if timeouts:
        return GateResult(False, GATE_CASE_TIMEOUT, f"{timeouts} case(s) did not terminate")
    if differences_outside_patterns:
        return GateResult(
            False,
            GATE_DIFFERENT_OUTSIDE_PATTERNS,
            f"{differences_outside_patterns} difference(s) outside the allowed patterns",
        )
    if suite_failures:
        return GateResult(False, GATE_SUITE_FAILURE, f"{suite_failures} suite failure(s)")
    return GateResult(True, GATE_PASS)


def differences_outside(
    differences: Sequence[Mapping[str, Any]], allowed_patterns: Sequence[str]
) -> list[Mapping[str, Any]]:
    """Differences whose path is not one a migration is allowed to change.

    Pattern matching is by prefix on the JSON path, so `$.value.stderr` covers
    `$.value.stderr` exactly and `$.value.stdout` does not accidentally match
    `$.value.stdout_sha256`. A difference deeper than a declared pattern (for
    example `$.value.files['x.py'].sha256`) is *not* covered by a pattern for
    `$.value.files`, because file contents changing is the thing under test.
    """
    allowed = tuple(pattern.rstrip("*") for pattern in allowed_patterns)
    outside: list[Mapping[str, Any]] = []
    for difference in differences:
        path = str(difference.get("path") or "")
        if any(path == pattern or path.startswith(pattern + ".") for pattern in allowed):
            continue
        outside.append(difference)
    return outside
