"""Behavior observation, canonical encoding, and content-addressed storage.

The harness answers one question: *did the behaviour change?* It does not prove
equivalence, and nothing here tries to. What it does guarantee is that two
observations are compared as **bytes**: every value is turned into a canonical
JSON form by `encode()`, so `==` on the encoded text is the comparison, and its
sha256 is the identity used for storage and for the golden record.

Canonical rules (ADR 004, D2):

- object keys are sorted by their UTF-8 bytes;
- floats are tagged (`{"__float__": "1.5"}`) so NaN, -0.0 and precision cannot
  be lost to a JSON number round-trip;
- bytes are hex-tagged, sets are sorted-tagged, tuples are tagged (so a tuple
  and a list are different behaviour), mappings with non-string keys become a
  sorted pair list — and so does any mapping with a key that starts with `__`,
  because otherwise `{"__float__": "1.0"}` would encode exactly like the float
  `1.0` and the canonical form would be ambiguous;
- date/datetime/time/timedelta/Decimal/UUID/Path/Enum are tagged explicitly.
  They have no `__dict__`, so the object branch would call them "opaque" and a
  changed date would compare EQUAL — the parse clock probe found this;
- iterators are materialised and tagged, because "lazy" is not observable
  through a comparison and a partially consumed iterator would otherwise look
  identical to an empty one. Materialisation is BOUNDED (`ITERATOR_LIMIT`);
- exceptions are `{"__exception__": {...}}` with type, module and message;
- arbitrary objects become `{"__object__": {"class": ..., "attrs": ...}}` with
  sorted attribute names, never `repr()` (a repr can embed a memory address).
  An object with no attributes is **opaque** and is recorded as such: two
  different opaque values would otherwise compare equal.

Encoding is also bounded against hostile structure: cycles become
`{"__cycle__": ...}`, depth and node budgets are capped, and every cap that
fires is reported in the observation's flags rather than silently truncating.
"""

from __future__ import annotations

import collections.abc
import datetime
import decimal
import enum
import hashlib
import importlib
import json
import math
import pathlib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Anything that IS an iterator: list_iterator/tuple_iterator/generators/map/
# filter/zip/enumerate all satisfy the ABC, and `type(iter(()))` alone does not.
_ITERATOR_TYPES = (collections.abc.Iterator,)

# Materialising an iterator is UNBOUNDED work chosen by the target: parse 1.22.4
# yields an infinite sequence for `findall("{:tt}", "22:13:20")` (zero-width
# match at the end), which hung the first baseline for 900 s. The bound is the
# harness's, and the truncation is recorded rather than silently dropped.
# Bumped when the ENCODING changes, not when a case is added. The EncodedValue
# fix (checkpoint 11) changed every blob's bytes without changing any value, so
# v1 and v2 blobs are not interchangeable even though the case set is the same;
# the version is in the payload and in the blob path so the two can never mix.
OBSERVATION_SCHEMA_VERSION = 2

ITERATOR_LIMIT = 1000

# Structure bounds. A refactor can turn a list into a graph; the encoder must
# not recurse forever and must not walk a million nodes.
MAX_DEPTH = 50
MAX_NODES = 50_000


@dataclass
class _State:
    """Per-call encoder state: budget, cycle path, and what had to be capped."""

    nodes: int = 0
    path: set[int] = field(default_factory=set)
    opaque: bool = False
    truncated: bool = False
    limit: str | None = None

    def spend(self) -> bool:
        self.nodes += 1
        if self.nodes > MAX_NODES:
            self.limit = self.limit or "nodes"
            return False
        return True


def encode(value: Any) -> Any:
    """Canonical, JSON-serialisable form of `value`."""
    return encode_with_meta(value)[0]


def encode_with_meta(value: Any) -> tuple[Any, dict[str, Any]]:
    """Encode and report what the encoder had to do to it.

    `meta` is what the child stores as observation flags: `opaque` (a value with
    no stable identity), `truncated` (a bounded iterator), `limit` (a depth or
    node budget fired). Any of them makes the case **unsupported** rather than
    silently comparable.
    """
    state = _State()
    encoded = _enc(value, state, 0)
    meta = {
        "opaque": state.opaque,
        "truncated": state.truncated,
        "limit": state.limit,
        "nodes": state.nodes,
    }
    return encoded, meta


def _enc(value: Any, state: _State, depth: int) -> Any:
    if not state.spend():
        return {"__limit__": "nodes"}
    if depth > MAX_DEPTH:
        state.limit = state.limit or "depth"
        return {"__limit__": "depth"}
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            text = "nan"
        elif math.isinf(value):
            text = "inf" if value > 0 else "-inf"
        else:
            text = repr(value)  # repr is the shortest round-tripping form
        return {"__float__": text}
    if isinstance(value, bytes | bytearray):
        return {"__bytes__": bytes(value).hex()}
    if isinstance(value, tuple):
        return {"__tuple__": [_enc(item, state, depth + 1) for item in value]}
    if isinstance(value, set | frozenset):
        encoded = [_enc(item, state, depth + 1) for item in value]
        return {"__set__": sorted(encoded, key=canonical_key)}
    if isinstance(value, list):
        return [_enc(item, state, depth + 1) for item in value]
    if isinstance(value, dict):
        return _encode_mapping(value, state, depth)
    if isinstance(value, _ITERATOR_TYPES):
        return _encode_iterator(value, state, depth)
    if isinstance(value, BaseException):
        return encode_exception(value)
    # Scalars with a stable textual identity. Without these, `encode` would fall
    # through to the opaque-object branch and a changed DATE would compare equal
    # (found by the parse clock probe: datetime.date has no __dict__).
    real_datetime, real_date, real_time = _datetime_types()
    if isinstance(value, real_datetime):
        return {"__datetime__": value.isoformat()}
    if isinstance(value, real_date):
        return {"__date__": value.isoformat()}
    if isinstance(value, real_time):
        return {"__time__": value.isoformat()}
    if isinstance(value, datetime.timedelta):
        return {"__timedelta__": repr(value.total_seconds())}
    if isinstance(value, decimal.Decimal):
        return {"__decimal__": str(value)}
    if isinstance(value, uuid.UUID):
        return {"__uuid__": str(value)}
    if isinstance(value, pathlib.PurePath):
        return {"__path__": str(value)}
    if isinstance(value, enum.Enum):
        return {
            "__enum__": f"{type(value).__module__}.{type(value).__qualname__}.{value.name}",
            "value": _enc(value.value, state, depth + 1),
        }
    return _encode_object(value, state, depth)


def _encode_mapping(value: dict[Any, Any], state: _State, depth: int) -> Any:
    marker = id(value)
    if marker in state.path:
        return {"__cycle__": "mapping"}
    if all(isinstance(key, str) for key in value) and not any(
        key.startswith("__") for key in value
    ):
        state.path.add(marker)
        try:
            return {
                key: _enc(value[key], state, depth + 1)
                for key in sorted(value, key=lambda k: k.encode("utf-8"))
            }
        finally:
            state.path.discard(marker)
    # Non-string keys OR a dunder-looking key: the tagged forms are a namespace,
    # so a mapping that could collide with one is written as an explicit pair
    # list. Without this, {"__float__": "1.0"} would encode like the float 1.0.
    state.path.add(marker)
    try:
        pairs = [[_enc(key, state, depth + 1), _enc(item, state, depth + 1)]
                 for key, item in value.items()]
    finally:
        state.path.discard(marker)
    return {"__mapping__": sorted(pairs, key=canonical_key)}


def _encode_iterator(value: Any, state: _State, depth: int) -> Any:
    items: list[Any] = []
    for index, item in enumerate(value):
        if index >= ITERATOR_LIMIT:
            state.truncated = True
            encoded: dict[str, Any] = {"__iterator__": items, "truncated": True,
                                       "limit": ITERATOR_LIMIT}
            return encoded
        items.append(_enc(item, state, depth + 1))
    return {"__iterator__": items}


def _encode_object(value: Any, state: _State, depth: int) -> Any:
    marker = id(value)
    if marker in state.path:
        return {"__cycle__": type(value).__qualname__}
    cls = type(value)
    attrs: dict[str, Any] = {}
    for name in getattr(value, "__dict__", {}) or {}:
        attrs[name] = getattr(value, name)
    for slot in getattr(cls, "__slots__", ()) or ():
        if isinstance(slot, str) and hasattr(value, slot):
            attrs[slot] = getattr(value, slot)
    payload: dict[str, Any] = {"class": f"{cls.__module__}.{cls.__qualname__}"}
    if attrs:
        state.path.add(marker)
        try:
            payload["attrs"] = {
                name: _enc(attrs[name], state, depth + 1) for name in sorted(attrs)
            }
        finally:
            state.path.discard(marker)
    else:
        # No attributes, no stable textual identity: two different values of
        # this type would encode identically. Say so, and let the case be
        # classified as unsupported rather than compared.
        payload["opaque"] = True
        state.opaque = True
    return {"__object__": payload}


def encode_exception(exc: BaseException) -> dict[str, Any]:
    cls = type(exc)
    return {
        "__exception__": {
            "type": cls.__qualname__,
            "module": cls.__module__,
            "message": str(exc),
        }
    }


def _datetime_types() -> tuple[
    type[datetime.datetime], type[datetime.date], type[datetime.time]
]:
    """The REAL datetime classes.

    Under the determinism shim `datetime.datetime` and `datetime.date` are
    replaced with frozen subclasses, so `isinstance(value, datetime.date)` is
    False for a plain `date` the target returns — which silently turned a
    changed date into "opaque object". The shim keeps the originals reachable;
    use them.
    """
    try:
        # generated at run time (no stubs, and absent unless the shim ran first)
        shim = importlib.import_module("sitecustomize")
        return (
            shim.ORIGINAL_DATETIME,
            shim.ORIGINAL_DATE,
            shim.ORIGINAL_TIME,
        )
    except (ImportError, AttributeError):
        return datetime.datetime, datetime.date, datetime.time


def canonical_key(value: Any) -> bytes:
    """Sort key for anything: its canonical JSON, as UTF-8 bytes."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def canonical_json(value: Any) -> str:
    return json.dumps(encode(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EncodedValue:
    """A value that the child already encoded, so the parent must not re-encode.

    Encoding twice is not harmless: a tag dict such as `{"__float__": "1.5"}`
    has a dunder key, so a second pass escapes it through the mapping form and
    every tag ends up one level down — rules then see a dict where the target
    returned a number, and the comparator reports `$.value.__mapping__[0][1]`
    instead of `$.value`.
    """

    payload: Any


@dataclass(frozen=True)
class Observation:
    """What one case actually did. Measurements only — no timings, no paths."""

    case_id: str
    entry: str
    inputs: dict[str, Any]
    outcome: str  # "return" | "exception" | "unsupported"
    value: Any = None
    exception: dict[str, Any] | None = None
    stdout: str = ""
    stderr: str = ""
    controls: tuple[str, ...] = ()
    flags: dict[str, Any] = field(default_factory=dict)
    case_set_hash: str = ""

    def value_body(self) -> Any:
        """The encoded value, encoding it once if it arrived raw."""
        if isinstance(self.value, EncodedValue):
            return self.value.payload
        return None if self.value is None else encode(self.value)

    def body(self) -> dict[str, Any]:
        return {
            "observation_schema_version": OBSERVATION_SCHEMA_VERSION,
            "case_id": self.case_id,
            "entry": self.entry,
            "inputs": encode(self.inputs),
            "outcome": self.outcome,
            # keep the encoded value even when unsupported: "what was opaque"
            # is the diagnostic, and dropping it made the report unable to say.
            "value": self.value_body(),
            "exception": self.exception,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "controls": list(self.controls),
            "flags": self.flags,
            "case_set_hash": self.case_set_hash,
        }

    def fingerprint_body(self) -> dict[str, Any]:
        """The observation WITHOUT run metadata.

        `controls` and `case_set_hash` describe the run, not the behaviour: a
        shimmed and an unshimmed pass must be comparable, so the comparison hash
        excludes them while the stored blob keeps them.
        """
        body = self.body()
        body.pop("controls", None)
        body.pop("case_set_hash", None)
        return body

    @property
    def sha256(self) -> str:
        """Identity of the stored blob (includes run metadata)."""
        return hashlib.sha256(
            json.dumps(self.body(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @property
    def fingerprint(self) -> str:
        """Identity of the BEHAVIOUR (excludes run metadata)."""
        text = json.dumps(self.fingerprint_body(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @property
    def comparable(self) -> bool:
        """False when the harness could not observe the value faithfully."""
        if self.outcome == "unsupported":
            return False
        return not (self.flags.get("opaque") or self.flags.get("limit"))

    def to_json(self) -> str:
        return json.dumps(self.body(), sort_keys=True, separators=(",", ":"))


@dataclass
class BlobStore:
    """Content-addressed observations: `<root>/v<schema>/<case_set_hash[:8]>/...`.

    The case-set hash is part of the PATH: a golden blob is only meaningful for
    the case set that produced it, so it cannot be silently reused across a
    changed case set.
    """

    root: Path
    written: set[str] = field(default_factory=set)

    def put(self, observation: Observation) -> str:
        key = observation.sha256
        shard = observation.case_set_hash[:8] or "unknown"
        path = self.root / f"v{OBSERVATION_SCHEMA_VERSION}" / shard / key[:2] / f"{key}.json"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(observation.to_json() + "\n", encoding="utf-8")
        self.written.add(key)
        return key
