"""Normalizer rules: declarative, versioned, validated, and narrow.

A rule exists for exactly one reason: the A/A baseline showed that a field is
noise, and a human decided that noise is not behaviour. Nothing else earns a
rule. So every rule carries a `reason` — the A/A finding or case that motivated
it — and the loader refuses a rule without one.

The rules are data, not code: one JSON file per target under `harness/rules/`,
validated against `harness/rules/schema.json` on load, hashed, and pinned in the
report it was used for. A rules file whose `case_set_hash` does not match the run
is refused, because a rule written for one case set is not a rule for another.

Path syntax is dotted (`$.value.created`, `*` for one segment). A target key that
itself contains `.` or `*` therefore cannot be named by a rule; that is a
deliberate fail-safe direction — the case stays `different` rather than being
masked by a path that means something else.

Masking is shape-checked before it happens. A UUID rule accepts only two UUID
shaped strings; a float rule accepts only two floats; a timestamp rule re-parses
both sides. That way a rule can absorb the *value* of a field without absorbing
a change of its *type*, and a change anywhere else in the observation is still
reported as different.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

RULE_TYPES = ("uuid", "timestamp", "float", "regex_mask", "unordered_list")

MASK_TAG = "__masked__"


class RuleError(ValueError):
    """A rules file that is invalid, or that belongs to another case set."""


def _schema_path() -> Path:
    return Path(__file__).resolve().parents[2] / "harness" / "rules" / "schema.json"


def schema() -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(_schema_path().read_text(encoding="utf-8"))
    return payload


def _validate(instance: Any, schema_node: dict[str, Any], path: str = "$") -> None:
    """Minimal JSON-Schema validation: the subset `schema.json` actually uses."""
    kind = schema_node.get("type")
    if kind == "object":
        if not isinstance(instance, dict):
            raise RuleError(f"{path}: expected an object")
        for name in schema_node.get("required", []):
            if name not in instance:
                raise RuleError(f"{path}: missing required field {name!r}")
        properties = schema_node.get("properties", {})
        if schema_node.get("additionalProperties") is False:
            extra = sorted(set(instance) - set(properties))
            if extra:
                raise RuleError(f"{path}: unknown field(s) {extra}")
        for name, value in instance.items():
            if name in properties:
                _validate(value, properties[name], f"{path}.{name}")
    elif kind == "array":
        if not isinstance(instance, list):
            raise RuleError(f"{path}: expected an array")
        item = schema_node.get("items", {})
        for index, value in enumerate(instance):
            _validate(value, item, f"{path}[{index}]")
        if "minItems" in schema_node and len(instance) < schema_node["minItems"]:
            raise RuleError(f"{path}: needs at least {schema_node['minItems']} item(s)")
    elif kind == "string":
        if not isinstance(instance, str):
            raise RuleError(f"{path}: expected a string")
        if "minLength" in schema_node and len(instance) < schema_node["minLength"]:
            raise RuleError(f"{path}: shorter than {schema_node['minLength']} character(s)")
        if "enum" in schema_node and instance not in schema_node["enum"]:
            raise RuleError(f"{path}: {instance!r} is not one of {schema_node['enum']}")
    elif kind == "number":
        if isinstance(instance, bool) or not isinstance(instance, (int, float)):
            raise RuleError(f"{path}: expected a number")
    elif kind == "boolean":
        if not isinstance(instance, bool):
            raise RuleError(f"{path}: expected a boolean")


@dataclass(frozen=True)
class Rule:
    id: str
    path: str
    type: str
    reason: str
    tolerance: float = 0.0
    tolerance_seconds: float = 0.0
    pattern: str = ""

    def matches(self, segments: tuple[str, ...]) -> bool:
        return _path_matches(self.path, segments)


def _segments(path: str) -> tuple[str, ...]:
    """Split a dotted path, KEEPING empty segments so `$.a..b` is refused.

    Dropping empties would make `$.value..key` silently mean `$.value.key`,
    i.e. a typo would mask a different field than the one named.
    """
    if path.startswith("$."):
        body = path[2:]
    elif path.startswith("$"):
        body = path[1:]
    else:
        body = path
    return tuple(body.split(".")) if body else ()


def _path_matches(rule_path: str, segments: tuple[str, ...]) -> bool:
    pattern = _segments(rule_path)
    if len(pattern) != len(segments):
        return False
    return all(want == "*" or want == have for want, have in zip(pattern, segments, strict=True))


@dataclass(frozen=True)
class RuleSet:
    target: str
    case_set_hash: str
    rules: tuple[Rule, ...] = ()
    sha256: str = ""
    source: str = ""

    def as_mapping(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "case_set_hash": self.case_set_hash,
            "rules": [
                {
                    key: value
                    for key, value in (
                        ("id", rule.id),
                        ("path", rule.path),
                        ("type", rule.type),
                        ("reason", rule.reason),
                        ("tolerance", rule.tolerance),
                        ("tolerance_seconds", rule.tolerance_seconds),
                        ("pattern", rule.pattern),
                    )
                    if value not in ("", 0.0)
                }
                for rule in self.rules
            ],
        }


def _canonical_hash(payload: dict[str, Any]) -> str:
    import hashlib

    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_rules(path: Path, *, case_set_hash: str = "") -> RuleSet:
    """Load and validate a rules file; refuse one that belongs elsewhere."""
    if not path.exists():
        return RuleSet(target="", case_set_hash=case_set_hash, source=str(path))
    payload = json.loads(path.read_text(encoding="utf-8"))
    _validate(payload, schema())
    rules: list[Rule] = []
    seen: set[str] = set()
    for entry in payload["rules"]:
        if entry["id"] in seen:
            raise RuleError(f"duplicate rule id {entry['id']!r}")
        seen.add(entry["id"])
        segments = _segments(entry["path"])
        if not entry["path"].startswith("$") or not segments or "" in segments:
            raise RuleError(
                f"{entry['id']}: path {entry['path']!r} must start with '$' and "
                "have no empty segment (a key containing '.' cannot be addressed)"
            )
        if entry["type"] == "regex_mask":
            try:
                import re

                re.compile(entry["pattern"])
            except Exception as exc:  # noqa: BLE001 - a bad pattern must be a clear refusal
                raise RuleError(f"{entry['id']}: pattern does not compile: {exc}") from exc
        rules.append(
            Rule(
                id=entry["id"],
                path=entry["path"],
                type=entry["type"],
                reason=entry["reason"],
                tolerance=float(entry.get("tolerance", 0.0)),
                tolerance_seconds=float(entry.get("tolerance_seconds", 0.0)),
                pattern=entry.get("pattern", ""),
            )
        )
    declared = payload.get("case_set_hash", "")
    if case_set_hash and declared and declared != case_set_hash:
        raise RuleError(
            "rules file was written for case set "
            f"{declared[:12]}, this run is {case_set_hash[:12]}: refusing to "
            "normalize a comparison with rules that were not written for it"
        )
    return RuleSet(
        target=payload["target"],
        case_set_hash=declared or case_set_hash,
        rules=tuple(rules),
        sha256=_canonical_hash(payload),
        source=str(path),
    )


# --- shape checks -----------------------------------------------------------


_TAGS = {
    "__float__": "float",
    "__datetime__": "timestamp",
    "__date__": "timestamp",
    "__time__": "timestamp",
}


def _decoded(value: Any) -> tuple[Any, str]:
    """Unwrap an encoder tag, so a rule sees the value rather than its envelope.

    The encoder writes `1.5` as `{"__float__": "1.5"}` and a timestamp as
    `{"__datetime__": "..."}`. A rule that only understood bare JSON would have
    to target the tag itself, which is the encoder's business, not the target's.
    Returns `(value, tag_kind)`; `tag_kind` is "" for untagged values.
    """
    if isinstance(value, dict) and len(value) == 1:
        (key, payload), = value.items()
        kind = _TAGS.get(key)
        if kind == "float" and isinstance(payload, str):
            try:
                return float(payload), kind
            except ValueError:
                return value, ""
        if kind == "timestamp" and isinstance(payload, str):
            return payload, kind
    return value, ""


def _shape_ok(rule: Rule, left: Any, right: Any) -> str:
    """Return "" when the mask is allowed, or the reason it is not."""
    if rule.type == "uuid":
        for side in (left, right):
            if not isinstance(side, str):
                return f"uuid rule needs strings, got {type(side).__name__}"
            try:
                UUID(side)
            except ValueError:
                return f"uuid rule: {side!r} is not a UUID"
        return ""
    if rule.type == "timestamp":
        parsed = []
        for side, _kind in (_decoded(left), _decoded(right)):
            if not isinstance(side, str):
                return f"timestamp rule needs strings, got {type(side).__name__}"
            try:
                parsed.append(datetime.fromisoformat(side))
            except ValueError:
                return f"timestamp rule: {side!r} is not ISO 8601"
        delta = abs((parsed[0] - parsed[1]).total_seconds())
        if delta > rule.tolerance_seconds:
            return (
                f"timestamp rule: {delta:.3f}s apart, tolerance is "
                f"{rule.tolerance_seconds:.3f}s"
            )
        return ""
    if rule.type == "float":
        left, left_kind = _decoded(left)
        right, right_kind = _decoded(right)
        for side, kind in ((left, left_kind), (right, right_kind)):
            if kind == "timestamp":
                return "float rule got a timestamp"
            if isinstance(side, bool) or not isinstance(side, float):
                return f"float rule needs floats, got {type(side).__name__}"
            if math.isnan(side):
                return "float rule: NaN has no tolerance"
        delta = abs(left - right)
        allowed = max(rule.tolerance, rule.tolerance * max(abs(left), abs(right)))
        if delta > allowed:
            return f"float rule: {delta!r} apart, tolerance is {allowed!r}"
        return ""
    if rule.type == "regex_mask":
        import re

        for side in (left, right):
            if not isinstance(side, str):
                return f"regex_mask rule needs strings, got {type(side).__name__}"
            if re.fullmatch(rule.pattern, side) is None:
                return f"regex_mask rule: {side!r} does not fully match {rule.pattern!r}"
        return ""
    if rule.type == "unordered_list":
        if not isinstance(left, list) or not isinstance(right, list):
            return "unordered_list rule needs lists"
        if len(left) != len(right):
            return f"unordered_list rule: lengths differ ({len(left)} != {len(right)})"
        left_keys = sorted(json.dumps(item, sort_keys=True) for item in left)
        right_keys = sorted(json.dumps(item, sort_keys=True) for item in right)
        if left_keys != right_keys:
            return "unordered_list rule: not equal as multisets"
        return ""
    return f"unknown rule type {rule.type!r}"


def _mask(rule: Rule) -> dict[str, Any]:
    return {MASK_TAG: {"type": rule.type, "rule": rule.id}}


@dataclass
class Applied:
    """One masked path, for the report: which rule, and why it was allowed."""

    rule_id: str
    path: str
    type: str
    reason: str


@dataclass
class Normalized:
    left: Any
    right: Any
    applied: list[Applied] = field(default_factory=list)
    refused: list[dict[str, str]] = field(default_factory=list)


def apply_rules(
    left: Any, right: Any, rules: RuleSet, path: str = "$"
) -> Normalized:
    """Walk both trees together, replacing rule-matched leaves on both sides.

    A match whose shapes disagree is NOT masked: it is recorded in `refused`, and
    the values stay as they were so the comparator reports the difference. A rule
    that cannot be checked cannot be used to hide anything.
    """
    result = Normalized(left=left, right=right)
    _walk(left, right, rules, _segments(path) if path != "$" else (), path, result)
    return result


def _walk(
    left: Any,
    right: Any,
    rules: RuleSet,
    segments: tuple[str, ...],
    path: str,
    out: Normalized,
) -> Any:
    for rule in rules.rules:
        if rule.matches(segments):
            problem = _shape_ok(rule, left, right)
            if problem:
                # A refused rule must not stop the walk: a broader rule that
                # does not fit this node must not shadow a narrower one that
                # does (a `$.value` uuid rule must not block `$.value.id`).
                out.refused.append({"rule_id": rule.id, "path": path, "reason": problem})
                continue
            out.applied.append(Applied(rule.id, path, rule.type, rule.reason))
            return (_mask(rule), _mask(rule))
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(set(left) | set(right)):
            one, other = _walk(
                left.get(key), right.get(key), rules, (*segments, key), f"{path}.{key}", out
            )
            left[key], right[key] = one, other
        return (left, right)
    if isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
        for index, (one, other) in enumerate(zip(left, right, strict=True)):
            new_left, new_right = _walk(
                one, other, rules, (*segments, "*"), f"{path}[{index}]", out
            )
            left[index], right[index] = new_left, new_right
    return (left, right)
