"""Properties of the comparator and the normalizer, not examples.

Examples pin down cases we thought of. These properties pin down what must hold
for values we did not think of:

* `compare(a, a)` is identical, for any observation.
* `compare` is symmetric: swapping the two runs swaps left and right, never the
  verdict.
* A difference outside a masked field is never absorbed by the rules — the one
  property that protects the whole "equivalent" class from being a lie.
* Normalisation is idempotent: applying the rules twice equals applying them
  once, so a rerun cannot drift.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from harness.compare import (
    CLASS_DIFFERENT,
    CLASS_EQUIVALENT,
    CLASS_IDENTICAL,
    CLASS_UNSUPPORTED,
    compare,
)
from harness.observation import Observation, encode_with_meta
from harness.rules import RuleSet, apply_rules, load_rules

CASE_SET = "c" * 64

LEAVES = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(-(10**12), 10**12),
    st.floats(allow_nan=False, allow_infinity=False, width=32),
    st.text(max_size=12),
    st.binary(max_size=8),
)

VALUES = st.recursive(
    LEAVES,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.text(max_size=6), children, max_size=4),
        st.tuples(children, children),
    ),
    max_leaves=12,
)


def _observation(value: Any, case_id: str = "prop-0001") -> Observation:
    encoded, meta = encode_with_meta(value)
    return Observation(
        case_id=case_id,
        entry="parse",
        inputs={"args": [], "kwargs": {}},
        outcome="return",
        value=encoded,
        flags={
            "opaque": bool(meta["opaque"]),
            "truncated": bool(meta["truncated"]),
            "limit": meta["limit"],
            "nodes": meta["nodes"],
        },
        case_set_hash=CASE_SET,
    )


def _rules(tmp_path: Any, entries: list[dict[str, Any]]) -> RuleSet:
    path = tmp_path / "rules.json"
    path.write_text(
        json.dumps({"target": "prop", "case_set_hash": CASE_SET, "rules": entries}),
        encoding="utf-8",
    )
    return load_rules(path, case_set_hash=CASE_SET)


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(VALUES)
def test_compare_with_itself_is_identical(value: Any) -> None:
    observation = _observation(value)
    verdict = compare(observation, _observation(value))
    assert verdict.report_class in (CLASS_IDENTICAL, CLASS_UNSUPPORTED)


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(VALUES, VALUES)
def test_comparison_is_symmetric(left: Any, right: Any) -> None:
    one = compare(_observation(left), _observation(right))
    other = compare(_observation(right), _observation(left))
    assert one.outcome == other.outcome
    assert one.path == other.path


# Keys without "." or "*": the rule path language is dotted, and a key that
# contains a separator cannot be named (see rules.py). That limit is asserted
# separately in tests/test_harness_rules.py; here the strategy stays inside it.
KEYS = st.text(alphabet="abcdefghij", min_size=1, max_size=6)
STRINGS = st.dictionaries(KEYS, st.text(max_size=8), min_size=2, max_size=4)


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(STRINGS, st.text(min_size=1, max_size=6))
def test_a_change_outside_the_masked_fields_is_never_absorbed(
    payload: dict[str, str], changed_key_seed: str
) -> None:
    """Mask every key but one; a change in that one key must stay different.

    This is the property that keeps `equivalent` honest: a rule may absorb the
    fields it names, and nothing else. If a change in an unnamed field could be
    absorbed, "equivalent" would be a claim about values the rules never saw.
    """
    keys = sorted(payload)
    keep = keys[hash(changed_key_seed) % len(keys)]
    rules = _rules(
        _tmp(),
        [
            {
                "id": f"mask-{key}",
                "path": f"$.value.{key}",
                "type": "regex_mask",
                "reason": "property: every field except the changed one is masked",
                # (?s) so the pattern matches newlines too: `.*` alone does not,
                # and a refused mask would make this property fail for the
                # wrong reason.
                "pattern": "(?s).*",
            }
            for key in keys
            if key != keep
        ],
    )
    left = dict(payload)
    right = copy.deepcopy(payload)
    right[keep] = payload[keep] + "-changed"

    verdict = compare(_observation(left), _observation(right), rules)
    assert verdict.report_class == CLASS_DIFFERENT, verdict
    assert verdict.path == f"$.value.{keep}", verdict.path


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(STRINGS)
def test_a_rule_that_names_every_changed_field_does_absorb_it(
    payload: dict[str, str]
) -> None:
    """The other half: masking the changed field DOES make it equivalent.

    Without this, a normalizer that never masks anything would pass the
    previous property.
    """
    keys = sorted(payload)
    rules = _rules(
        _tmp(),
        [
            {
                "id": f"mask-{key}",
                "path": f"$.value.{key}",
                "type": "regex_mask",
                "reason": "property: every field is declared noise",
                # (?s) so the pattern matches newlines too: `.*` alone does not,
                # and a refused mask would make this property fail for the
                # wrong reason.
                "pattern": "(?s).*",
            }
            for key in keys
        ],
    )
    left = dict(payload)
    right = copy.deepcopy(payload)
    right[keys[0]] = "changed"
    verdict = compare(_observation(left), _observation(right), rules)
    assert verdict.report_class == CLASS_EQUIVALENT, verdict
    assert verdict.masked, "the report must name the rules that were applied"


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(st.text(max_size=6), max_size=5))
def test_normalisation_is_idempotent(items: list[str]) -> None:
    """apply(apply(x)) == apply(x), for both sides of a comparison."""
    rules = _rules(
        _tmp(),
        [{"id": "mask", "path": "$.value", "type": "unordered_list",
          "reason": "property: list order is not behaviour"}],
    )
    left = _observation(list(items)).value_body()
    right = _observation(list(reversed(items))).value_body()
    once = apply_rules(copy.deepcopy(left), copy.deepcopy(right), rules)
    twice = apply_rules(copy.deepcopy(once.left), copy.deepcopy(once.right), rules)
    assert json.dumps(twice.left, sort_keys=True) == json.dumps(once.left, sort_keys=True)
    assert json.dumps(twice.right, sort_keys=True) == json.dumps(once.right, sort_keys=True)


def _tmp() -> Any:
    import tempfile
    from pathlib import Path

    return Path(tempfile.mkdtemp())
