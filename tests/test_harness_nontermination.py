"""Non-termination is a behaviour change, not an unobservable one.

A mutant that makes a case loop forever differs from one that returns: the suite
kills it by timeout. The harness used to call it `undecidable`, because
`unsupported` versus `return` says nothing about *what* was unsupported — so a
whole class of real behaviour changes counted as neither killed nor survived.

The rule, from the review: a case that terminated on one tree and exceeded its
budget on two repeated runs on the other is **different**, provided the side that
terminated used at most 10 % of the same budget. Everything else — one timeout, a
slow baseline, both sides timing out — stays `unsupported`, because those are the
ambiguous cases the class exists for.

`int_minus@325:24` is the example this was written for: `line_idx += 1` becomes
`-= 1` in the E303 blank-line search, so `fixture-e25` and `fixture-e30not` never
return.
"""

from __future__ import annotations

from harness.compare import (
    CLASS_DIFFERENT,
    CLASS_IDENTICAL,
    CLASS_UNSUPPORTED,
    compare,
)
from harness.observation import EncodedValue, Observation


def obs(
    case_id: str = "fixture-e25",
    *,
    outcome: str = "return",
    limits: dict[str, object] | None = None,
    value: object = None,
) -> Observation:
    return Observation(
        case_id=case_id,
        entry="pycodestyle.py",
        inputs={"argv": ["E25.py"]},
        outcome=outcome,
        value=EncodedValue(value if value is not None else {"exit": 0, "files": {}}),
        limits=dict(limits or {}),
    )


def timed_out(budget: float = 20.0, *, twice: bool = True) -> dict[str, object]:
    limits: dict[str, object] = {"budget_seconds": budget, "elapsed_seconds": budget}
    if twice:
        limits["budget_exceeded_twice"] = True
    return limits


def finished(budget: float = 20.0, elapsed: float = 0.6) -> dict[str, object]:
    return {"budget_seconds": budget, "elapsed_seconds": elapsed}


def test_a_double_timeout_against_a_fast_return_is_a_difference() -> None:
    result = compare(
        obs(limits=finished()), obs(outcome="unsupported", limits=timed_out())
    )
    assert result.report_class == CLASS_DIFFERENT
    assert "non-termination" in (result.reason or "")
    assert "two repeated runs" in (result.reason or "")


def test_the_same_rule_applies_in_either_direction() -> None:
    result = compare(
        obs(outcome="unsupported", limits=timed_out()), obs(limits=finished())
    )
    assert result.report_class == CLASS_DIFFERENT


def test_one_timeout_is_still_unsupported() -> None:
    """A single timeout can be a loaded machine, so it is not evidence."""
    result = compare(
        obs(limits=finished()),
        obs(outcome="unsupported", limits=timed_out(twice=False)),
    )
    assert result.report_class == CLASS_UNSUPPORTED


def test_a_slow_baseline_makes_the_timeout_ambiguous() -> None:
    """If the returning side used 40 % of its budget, 10 % is not the story."""
    result = compare(
        obs(limits=finished(elapsed=8.0)),
        obs(outcome="unsupported", limits=timed_out()),
    )
    assert result.report_class == CLASS_UNSUPPORTED


def test_both_sides_timing_out_is_unsupported() -> None:
    result = compare(
        obs(outcome="unsupported", limits=timed_out()),
        obs(outcome="unsupported", limits=timed_out()),
    )
    assert result.report_class == CLASS_UNSUPPORTED


def test_no_budget_recorded_means_no_rule() -> None:
    """Old blobs predate `limits`; they must keep their old classification."""
    result = compare(obs(), obs(outcome="unsupported"))
    assert result.report_class == CLASS_UNSUPPORTED


def test_timing_is_not_in_the_compared_value() -> None:
    """`limits` is metadata: two runs that differ only in timing are identical."""
    left = obs(limits=finished(elapsed=0.4))
    right = obs(limits=finished(elapsed=0.9))
    left_value = obs(limits=finished(elapsed=0.4))
    assert left.fingerprint == left_value.fingerprint
    assert compare(left, right).report_class == CLASS_IDENTICAL

    # ...and the limits are still stored for the non-termination rule.
    assert left.body()["limits"]["elapsed_seconds"] == 0.4
    assert "limits" not in left.fingerprint_body()
