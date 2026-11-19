"""The rules loader: schema, hash pinning, reasons, and what it refuses."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.rules import MASK_TAG, RuleError, apply_rules, load_rules, schema

CASE_SET = "d" * 64


def _write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _rule(**overrides: object) -> dict:
    base = {
        "id": "r1",
        "path": "$.value",
        "type": "uuid",
        "reason": "A/A: uuid4 differs across passes without uuid_frozen",
    }
    base.update(overrides)
    return base


def test_a_valid_file_loads_and_is_hashed(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {"target": "parse", "case_set_hash": CASE_SET, "rules": [_rule()]},
    )
    rules = load_rules(path, case_set_hash=CASE_SET)
    assert rules.rules[0].id == "r1"
    assert len(rules.sha256) == 64
    assert rules.target == "parse"


def test_the_shipped_schema_is_valid_json_with_the_rule_types(tmp_path: Path) -> None:
    document = schema()
    assert document["required"] == ["target", "case_set_hash", "rules"]
    allowed = set(document["properties"]["rules"]["items"]["properties"]["type"]["enum"])
    assert allowed == {"uuid", "timestamp", "float", "regex_mask", "unordered_list"}


def test_a_rule_without_a_reason_is_refused(tmp_path: Path) -> None:
    entry = _rule()
    del entry["reason"]
    path = _write(tmp_path, {"target": "t", "case_set_hash": CASE_SET, "rules": [entry]})
    with pytest.raises(RuleError, match="missing required field 'reason'"):
        load_rules(path)


def test_a_short_reason_is_refused(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {"target": "t", "case_set_hash": CASE_SET, "rules": [_rule(reason="noise")]},
    )
    with pytest.raises(RuleError, match="shorter than 12"):
        load_rules(path)


def test_an_unknown_rule_type_is_refused(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {"target": "t", "case_set_hash": CASE_SET, "rules": [_rule(type="everything")]},
    )
    with pytest.raises(RuleError, match="is not one of"):
        load_rules(path)


def test_an_unknown_field_is_refused(tmp_path: Path) -> None:
    entry = _rule()
    entry["why"] = "a typo must not be silently ignored"
    path = _write(tmp_path, {"target": "t", "case_set_hash": CASE_SET, "rules": [entry]})
    with pytest.raises(RuleError, match="unknown field"):
        load_rules(path)


def test_rules_for_another_case_set_are_refused(tmp_path: Path) -> None:
    path = _write(
        tmp_path, {"target": "t", "case_set_hash": "e" * 64, "rules": [_rule()]}
    )
    with pytest.raises(RuleError, match="refusing to normalize"):
        load_rules(path, case_set_hash=CASE_SET)


def test_a_duplicate_rule_id_is_refused(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {"target": "t", "case_set_hash": CASE_SET, "rules": [_rule(), _rule()]},
    )
    with pytest.raises(RuleError, match="duplicate rule id"):
        load_rules(path)


def test_a_path_with_an_empty_segment_is_refused(tmp_path: Path) -> None:
    """A key containing '.' cannot be addressed, so such a rule is refused."""
    path = _write(
        tmp_path,
        {"target": "t", "case_set_hash": CASE_SET, "rules": [_rule(path="$.value..key")]},
    )
    with pytest.raises(RuleError, match="empty segment"):
        load_rules(path)


def test_a_bad_regex_is_refused_at_load_not_at_compare(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {
            "target": "t",
            "case_set_hash": CASE_SET,
            "rules": [_rule(type="regex_mask", pattern="([unclosed")],
        },
    )
    with pytest.raises(RuleError, match="does not compile"):
        load_rules(path)


def test_uuid_rule_masks_only_uuid_shaped_strings(tmp_path: Path) -> None:
    path = _write(tmp_path, {"target": "t", "case_set_hash": CASE_SET, "rules": [_rule()]})
    rules = load_rules(path)
    left = {"value": "5eed0000-0000-0000-0000-000000000001"}
    right = {"value": "5eed0000-0000-0000-0000-000000000002"}
    applied = apply_rules(left, right, rules)
    assert applied.applied and applied.applied[0].rule_id == "r1"
    assert applied.left == {"value": {MASK_TAG: {"type": "uuid", "rule": "r1"}}}

    refused = apply_rules({"value": "not-a-uuid"}, {"value": "also-not"}, rules)
    assert not refused.applied
    assert "is not a UUID" in refused.refused[0]["reason"]


def test_timestamp_rule_respects_its_tolerance(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {
            "target": "t",
            "case_set_hash": CASE_SET,
            "rules": [_rule(type="timestamp", tolerance_seconds=5)],
        },
    )
    rules = load_rules(path)
    ok = apply_rules(
        {"value": {"__datetime__": "2023-11-14T22:13:20"}},
        {"value": {"__datetime__": "2023-11-14T22:13:23"}},
        rules,
    )
    assert ok.applied
    far = apply_rules(
        {"value": {"__datetime__": "2023-11-14T22:13:20"}},
        {"value": {"__datetime__": "2023-11-14T22:14:20"}},
        rules,
    )
    assert not far.applied
    assert "tolerance" in far.refused[0]["reason"]


def test_float_rule_rejects_an_int_against_a_float(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {"target": "t", "case_set_hash": CASE_SET, "rules": [_rule(type="float", tolerance=1e-9)]},
    )
    rules = load_rules(path)
    refused = apply_rules({"value": 1}, {"value": {"__float__": "1.0"}}, rules)
    assert not refused.applied
    assert "float rule needs floats" in refused.refused[0]["reason"]


def test_unordered_list_rule_compares_multisets(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {"target": "t", "case_set_hash": CASE_SET, "rules": [_rule(type="unordered_list")]},
    )
    rules = load_rules(path)
    assert apply_rules({"value": ["a", "b"]}, {"value": ["b", "a"]}, rules).applied
    refused = apply_rules({"value": ["a", "b"]}, {"value": ["a", "c"]}, rules)
    assert not refused.applied
    assert "multisets" in refused.refused[0]["reason"]
    shorter = apply_rules({"value": ["a", "b"]}, {"value": ["a"]}, rules)
    assert not shorter.applied


def test_a_wildcard_rule_masks_one_segment(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {
            "target": "t",
            "case_set_hash": CASE_SET,
            "rules": [_rule(type="regex_mask", path="$.value.*", pattern="[0-9]+")],
        },
    )
    rules = load_rules(path)
    applied = apply_rules({"value": {"a": "1", "b": "2"}}, {"value": {"a": "3", "b": "4"}}, rules)
    assert len(applied.applied) == 2
