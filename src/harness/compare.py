"""Compare two observations of the same case.

Four outcomes, not two. The interesting one is the third: two observations that
were both cut off by the same bound and agree on everything inside it are
*equivalent on the bounded prefix*, which is not equality and must not be
reported as such — the values beyond the bound were never seen.

    equal                 same observable value, same outcome
    different             an observable difference, at a reported path
    equivalent_bounded    both truncated at the same bound, prefixes agree
    undecidable           the harness could not observe the value at all

`undecidable` is the default for anything the encoder flagged: opaque values,
limit hits, unknown outcomes. Folding those into `equal` would turn "I could not
look" into "nothing changed", which is the one mistake this harness exists to
avoid.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .observation import Observation
from .rules import RuleSet, apply_rules

EQUAL = "equal"
DIFFERENT = "different"
EQUIVALENT = "equivalent"
EQUIVALENT_BOUNDED = "equivalent_bounded"
UNDECIDABLE = "undecidable"

OUTCOMES = (EQUAL, DIFFERENT, EQUIVALENT, EQUIVALENT_BOUNDED, UNDECIDABLE)

# How the four comparison outcomes land in the five report classes. They are NOT
# one-to-one, so the report keeps five lines and never folds a hedged verdict
# into a confident one:
#
#   identical            -> equal, nothing masked
#   equivalent           -> equal after a declared rule masked a field
#   equivalent_bounded   -> both truncated at the same bound, prefixes agree
#   different            -> an observable difference, at a reported path
#   unsupported          -> undecidable: opaque, budget hit, or unsupported case
#   flaky                -> the case varied across A/A passes (assigned by the
#                           workflow, not by a comparison)
#
# `equivalent_bounded` stays on its own line, next to `equivalent` and never
# inside it. `undecidable` is counted with `unsupported` in the one-line
# summary, and every such case is listed with its reason.
CLASS_IDENTICAL = "identical"
CLASS_EQUIVALENT = "equivalent"
CLASS_EQUIVALENT_BOUNDED = "equivalent_bounded"
CLASS_DIFFERENT = "different"
CLASS_UNSUPPORTED = "unsupported"
CLASS_FLAKY = "flaky"

CLASSES = (
    CLASS_IDENTICAL,
    CLASS_EQUIVALENT,
    CLASS_EQUIVALENT_BOUNDED,
    CLASS_DIFFERENT,
    CLASS_UNSUPPORTED,
    CLASS_FLAKY,
)


@dataclass(frozen=True)
class Comparison:
    """One case's verdict, with the path that produced it when it differs."""

    case_id: str
    outcome: str
    path: str = ""
    left: Any = None
    right: Any = None
    reason: str = ""
    masked: tuple[str, ...] = ()
    applied: tuple[Any, ...] = ()

    @property
    def report_class(self) -> str:
        if self.outcome == EQUAL:
            return CLASS_EQUIVALENT if self.masked else CLASS_IDENTICAL
        return {
            DIFFERENT: CLASS_DIFFERENT,
            EQUIVALENT_BOUNDED: CLASS_EQUIVALENT_BOUNDED,
            UNDECIDABLE: CLASS_UNSUPPORTED,
        }[self.outcome]

    def __str__(self) -> str:
        head = f"{self.case_id}: {self.outcome}"
        if self.outcome == DIFFERENT:
            head += f" at {self.path} ({self.left!r} != {self.right!r})"
        return head + (f" [{self.reason}]" if self.reason else "")


def _first_difference(left: Any, right: Any, path: str = "$") -> tuple[str, Any, Any] | None:
    """Depth-first search for the first differing leaf, with a JSON-ish path."""
    if type(left) is not type(right):
        return path, left, right
    if isinstance(left, dict):
        for key in sorted(set(left) | set(right)):
            if key not in left or key not in right:
                return f"{path}.{key}", left.get(key), right.get(key)
            found = _first_difference(left[key], right[key], f"{path}.{key}")
            if found is not None:
                return found
        return None
    if isinstance(left, list):
        if len(left) != len(right):
            return f"{path}.length", len(left), len(right)
        for index, (one, other) in enumerate(zip(left, right, strict=True)):
            found = _first_difference(one, other, f"{path}[{index}]")
            if found is not None:
                return found
        return None
    if left != right:
        return path, left, right
    return None


def _behaviour(observation: Observation) -> dict[str, Any]:
    """Only what the target did.

    `flags` is excluded on purpose: a node count is bookkeeping, and reporting
    "different at $.flags.nodes" for a dict-versus-float change would name the
    wrong thing. Flags still decide `undecidable` and `equivalent_bounded`.
    """
    return {
        "inputs": observation.inputs,
        "outcome": observation.outcome,
        "value": observation.value_body(),
        "exception": observation.exception,
        "stdout": observation.stdout,
        "stderr": observation.stderr,
    }


# How much of its budget the returning side may have used and still count as
# "finished quickly" — the review's 10 %. A baseline that used most of its budget
# is a slow case, and a slow case timing out on the other tree is ambiguous.
NONTERMINATION_FAST_FRACTION = 0.10


def _budget_evidence(observation: Observation) -> dict[str, Any] | None:
    """The recorded budget facts, or None if the case never hit one."""
    limits = observation.limits or {}
    if not limits.get("budget_seconds"):
        return None
    return limits


def _nontermination(left: Observation, right: Observation) -> Comparison | None:
    """One tree terminated well inside its budget, the other exceeded it twice.

    A mutant that makes a case loop forever is a behaviour change, not an
    unobservable one: the suite kills it by timeout, and the harness used to call
    it `undecidable` because `unsupported` versus `return` says nothing about
    *what* was unsupported. The two pieces of evidence that make it a difference
    rather than a guess are both recorded by the child:

      * the side that hit the budget hit it on **two repeated runs** (a single
        timeout can be a loaded machine), and
      * the side that returned finished within 10 % of the same budget (a case
        that used most of its budget is slow, and a slow case timing out on the
        other tree is genuinely ambiguous).

    Anything else stays `undecidable`, which is what "keep unsupported for
    ambiguous timeouts" means in practice.
    """
    for finished, stuck in ((left, right), (right, left)):
        if finished.outcome != "return":
            continue
        evidence = _budget_evidence(finished)
        stuck_limits = stuck.limits or {}
        if not evidence or not stuck_limits.get("budget_exceeded_twice"):
            continue
        budget = float(evidence["budget_seconds"])
        elapsed = float(evidence.get("elapsed_seconds") or 0.0)
        if budget <= 0 or elapsed > budget * NONTERMINATION_FAST_FRACTION:
            continue
        return Comparison(
            left.case_id,
            DIFFERENT,
            reason=(
                "non-termination: the other side exceeded its "
                f"{budget:.0f}s budget on two repeated runs while this side "
                f"returned in {elapsed:.2f}s ({100 * elapsed / budget:.1f}% of budget)"
            ),
        )
    return None


def compare(
    left: Observation, right: Observation, rules: RuleSet | None = None
) -> Comparison:
    """Compare two observations of the SAME case under the same case set."""
    if left.case_id != right.case_id:
        raise ValueError(f"not the same case: {left.case_id} != {right.case_id}")
    if (
        left.case_set_hash
        and right.case_set_hash
        and left.case_set_hash != right.case_set_hash
    ):
        raise ValueError(
            "case-set hash mismatch: the two observations come from different "
            f"case sets ({left.case_set_hash[:12]} != {right.case_set_hash[:12]})"
        )

    nontermination = _nontermination(left, right)
    if nontermination is not None:
        return nontermination

    # Anything the encoder could not see is undecidable, whatever it contains.
    for observation in (left, right):
        flags = observation.flags or {}
        if observation.outcome == "unsupported":
            return Comparison(
                left.case_id, UNDECIDABLE, reason=f"{observation.entry} was unsupported"
            )
        if flags.get("opaque"):
            return Comparison(left.case_id, UNDECIDABLE, reason="opaque value")
        if flags.get("limit"):
            return Comparison(
                left.case_id, UNDECIDABLE, reason=f"{flags['limit']} budget hit"
            )

    left_flags = left.flags or {}
    right_flags = right.flags or {}
    truncated = bool(left_flags.get("truncated")) and bool(right_flags.get("truncated"))

    left_body, right_body = _behaviour(left), _behaviour(right)
    masked: tuple[str, ...] = ()
    applied_items: tuple[Any, ...] = ()
    refusals: list[str] = []
    if rules is not None and rules.rules:
        # Deep copies: normalisation must not rewrite the stored observations.
        import json

        applied = apply_rules(
            json.loads(json.dumps(left_body)), json.loads(json.dumps(right_body)), rules
        )
        left_body, right_body = applied.left, applied.right
        masked = tuple(f"{item.rule_id}@{item.path}" for item in applied.applied)
        applied_items = tuple(applied.applied)
        refusals = [f"{item['rule_id']}@{item['path']}: {item['reason']}"
                    for item in applied.refused]

    difference = _first_difference(left_body, right_body)
    if difference is None:
        if truncated:
            return Comparison(
                left.case_id,
                EQUIVALENT_BOUNDED,
                reason="both truncated; the prefixes agree and nothing beyond was seen",
                masked=masked,
                applied=applied_items,
            )
        return Comparison(
            left.case_id,
            EQUAL,
            masked=masked,
            applied=applied_items,
            reason=("masked by " + ", ".join(masked)) if masked else "",
        )
    refused = ("; refused: " + "; ".join(refusals)) if refusals else ""
    if truncated:
        # A difference inside a truncated value is still a real difference.
        return Comparison(
            left.case_id, DIFFERENT, *difference,
            reason="truncated but differs" + refused, masked=masked,
            applied=applied_items,
        )
    return Comparison(
        left.case_id, DIFFERENT, *difference, reason=refused, masked=masked,
        applied=applied_items,
    )


def summarise(comparisons: list[Comparison]) -> dict[str, Any]:
    """Counts by report class, with every undecidable case named and explained.

    `unsupported` folds in the undecidable verdicts — the one-line summary has
    five classes, not six — but the individual reasons are always kept next to
    the count so the fold cannot be mistaken for "nothing to see".
    """
    counts = {name: 0 for name in CLASSES}
    reasons: list[dict[str, str]] = []
    for comparison in comparisons:
        counts[comparison.report_class] += 1
        if comparison.outcome == UNDECIDABLE:
            reasons.append({"case_id": comparison.case_id, "reason": comparison.reason})
    return {**counts, "undecidable_reasons": reasons}


def compare_sets(
    left: dict[str, Observation],
    right: dict[str, Observation],
    rules: RuleSet | None = None,
) -> dict[str, Comparison]:
    """Compare two `case_id -> observation` maps, refusing an incomplete pair."""
    missing = sorted(set(left) ^ set(right))
    if missing:
        raise ValueError(f"case sets differ: {missing[:5]}")
    return {
        case_id: compare(left[case_id], right[case_id], rules) for case_id in sorted(left)
    }
