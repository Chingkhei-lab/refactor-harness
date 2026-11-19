"""Comparator tests, written before the comparator (checkpoint 11 order).

The four cases here are the ones a naive comparator gets wrong:

1. a mapping that *looks* like a tagged float,
2. an object with no attributes at all,
3. an object that refers to itself,
4. two observations that were both cut off, with matching prefixes.

Case 4 is the reason the comparator has four outcomes instead of two.
"""

from __future__ import annotations

import pytest

from harness.compare import (
    DIFFERENT,
    EQUAL,
    EQUIVALENT_BOUNDED,
    UNDECIDABLE,
    compare,
    compare_sets,
)
from harness.observation import Observation, encode_with_meta


def _observation(case_id: str, value: object, *, outcome: str = "return") -> Observation:
    encoded, meta = encode_with_meta(value)
    flags = {
        "opaque": bool(meta["opaque"]),
        "truncated": bool(meta["truncated"]),
        "limit": meta["limit"],
        "nodes": meta["nodes"],
    }
    return Observation(
        case_id=case_id,
        entry="parse",
        inputs={"args": ["{}", "x"], "kwargs": {}},
        outcome=outcome,
        value=encoded,
        flags=flags,
        case_set_hash="a" * 64,
    )


def test_a_mapping_cannot_impersonate_a_tagged_float() -> None:
    """`{"__float__": "1.0"}` is a dict; float 1.0 is not. They must differ."""
    mapping = _observation("c1", {"__float__": "1.0"})
    number = _observation("c1", 1.0)

    assert compare(number, number).outcome == EQUAL
    verdict = compare(mapping, number)
    assert verdict.outcome == DIFFERENT
    assert verdict.path.startswith("$.value")


def test_an_opaque_object_is_undecidable_not_equal() -> None:
    """Two `re.Match` objects encode the same and mean nothing by it."""
    import re

    pattern = re.compile("a")
    left = _observation("c1", pattern.match("a"))
    right = _observation("c1", pattern.match("aa"))

    verdict = compare(left, right)
    assert verdict.outcome == UNDECIDABLE
    assert "opaque" in verdict.reason


def test_a_cyclic_value_cannot_be_recursed_and_is_undecidable() -> None:
    """Encoding terminates; the verdict must not pretend the value was seen.

    The back-edge is encoded as a marker naming the class, not the identity of
    the ancestor it points at, so a structure that retargets its back-edge is
    invisible to the comparator. Reporting `undecidable` is the only honest
    verdict; reporting `equal` would be a claim the encoding cannot support.
    """
    left: list[object] = []
    left.append(left)
    right: list[object] = []
    right.append(right)

    verdict = compare(_observation("c1", left), _observation("c1", right))
    assert verdict.outcome == UNDECIDABLE
    assert "opaque" in verdict.reason or "budget" in verdict.reason

    # and it must not hang, even when the shapes differ
    different: list[object] = []
    different.append([different])
    assert compare(_observation("c1", left), _observation("c1", different)).outcome == (
        UNDECIDABLE
    )


def test_two_truncated_observations_with_matching_prefixes_are_equivalent_on_prefix() -> None:
    """Same bound, same 1 000 items, unknown beyond it: NOT 'equal'."""
    left = _observation("c1", iter(range(5000)))
    right = _observation("c1", iter(range(5000)))
    assert left.flags["truncated"] and right.flags["truncated"]

    verdict = compare(left, right)
    assert verdict.outcome == EQUIVALENT_BOUNDED
    assert "nothing beyond" in verdict.reason


def test_two_truncated_observations_that_differ_inside_the_prefix_are_different() -> None:
    left = _observation("c1", iter(range(5000)))
    right = _observation("c1", iter([0, 1, 2, 999] + list(range(4, 5000))))

    verdict = compare(left, right)
    assert verdict.outcome == DIFFERENT


def test_a_limit_hit_is_undecidable() -> None:
    deep: list[object] = []
    node: list[object] = deep
    for _ in range(80):
        child: list[object] = []
        node.append(child)
        node = child

    verdict = compare(_observation("c1", deep), _observation("c1", deep))
    assert verdict.outcome == UNDECIDABLE
    assert "budget" in verdict.reason


def test_unsupported_is_undecidable() -> None:
    verdict = compare(
        _observation("c1", None, outcome="unsupported"),
        _observation("c1", None, outcome="unsupported"),
    )
    assert verdict.outcome == UNDECIDABLE


def test_comparing_different_case_sets_is_refused() -> None:
    left = _observation("c1", 1)
    right = Observation(
        case_id="c1", entry="parse", inputs={}, outcome="return", value=1,
        case_set_hash="b" * 64,
    )
    with pytest.raises(ValueError, match="case-set hash mismatch"):
        compare(left, right)


def test_compare_sets_refuses_a_missing_case() -> None:
    both = _observation("c1", 1)
    with pytest.raises(ValueError, match="case sets differ"):
        compare_sets({"c1": both}, {"c1": both, "c2": both})
