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

EQUAL = "equal"
DIFFERENT = "different"
EQUIVALENT_BOUNDED = "equivalent_bounded"
UNDECIDABLE = "undecidable"

OUTCOMES = (EQUAL, DIFFERENT, EQUIVALENT_BOUNDED, UNDECIDABLE)


@dataclass(frozen=True)
class Comparison:
    """One case's verdict, with the path that produced it when it differs."""

    case_id: str
    outcome: str
    path: str = ""
    left: Any = None
    right: Any = None
    reason: str = ""

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
        "value": observation.value,
        "exception": observation.exception,
        "stdout": observation.stdout,
        "stderr": observation.stderr,
    }


def compare(left: Observation, right: Observation) -> Comparison:
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

    difference = _first_difference(_behaviour(left), _behaviour(right))
    if difference is None:
        if truncated:
            return Comparison(
                left.case_id,
                EQUIVALENT_BOUNDED,
                reason="both truncated; the prefixes agree and nothing beyond was seen",
            )
        return Comparison(left.case_id, EQUAL)
    if truncated:
        # A difference inside a truncated value is still a real difference.
        return Comparison(left.case_id, DIFFERENT, *difference, reason="truncated but differs")
    return Comparison(left.case_id, DIFFERENT, *difference)


def compare_sets(
    left: dict[str, Observation], right: dict[str, Observation]
) -> dict[str, Comparison]:
    """Compare two `case_id -> observation` maps, refusing an incomplete pair."""
    missing = sorted(set(left) ^ set(right))
    if missing:
        raise ValueError(f"case sets differ: {missing[:5]}")
    return {case_id: compare(left[case_id], right[case_id]) for case_id in sorted(left)}
