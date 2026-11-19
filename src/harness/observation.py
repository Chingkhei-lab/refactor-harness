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
  sorted pair list;
- date/datetime/time/timedelta/Decimal/UUID/Path/Enum are tagged explicitly.
  They have no `__dict__`, so the object branch would call them "opaque" and a
  changed date would compare EQUAL — the parse clock probe found this;
- iterators are materialised and tagged, because "lazy" is not observable
  through a comparison and a partially consumed iterator would otherwise look
  identical to an empty one;
- exceptions are `{"__exception__": {...}}` with type, module and message;
- arbitrary objects become `{"__object__": {"class": ..., "attrs": ...}}` with
  sorted attribute names, never `repr()` (a repr can embed a memory address).
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
ITERATOR_LIMIT = 1000


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


def encode(value: Any) -> Any:
    """Canonical, JSON-serialisable form of `value`."""
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
        return {"__tuple__": [encode(item) for item in value]}
    if isinstance(value, set | frozenset):
        return {"__set__": sorted((encode(item) for item in value), key=canonical_key)}
    if isinstance(value, list):
        return [encode(item) for item in value]
    if isinstance(value, dict):
        return _encode_mapping(value)
    if isinstance(value, _ITERATOR_TYPES):
        # materialise, but only up to the bound: a lazy return value has no
        # stable identity otherwise, and an infinite generator must not hang
        # the harness (or be mistaken for a finite one).
        items: list[Any] = []
        truncated = False
        for index, item in enumerate(value):
            if index >= ITERATOR_LIMIT:
                truncated = True
                break
            items.append(encode(item))
        encoded: dict[str, Any] = {"__iterator__": items}
        if truncated:
            encoded["truncated"] = True
            encoded["limit"] = ITERATOR_LIMIT
        return encoded
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
            "value": encode(value.value),
        }
    return _encode_object(value)


def _encode_mapping(value: dict[Any, Any]) -> Any:
    if all(isinstance(key, str) for key in value):
        return {key: encode(value[key]) for key in sorted(value, key=lambda k: k.encode("utf-8"))}
    pairs = [[encode(key), encode(item)] for key, item in value.items()]
    return {"__mapping__": sorted(pairs, key=canonical_key)}


def _encode_object(value: Any) -> Any:
    cls = type(value)
    attrs: dict[str, Any] = {}
    for name in getattr(value, "__dict__", {}) or {}:
        attrs[name] = getattr(value, name)
    for slot in getattr(cls, "__slots__", ()) or ():
        if isinstance(slot, str) and hasattr(value, slot):
            attrs[slot] = getattr(value, slot)
    payload: dict[str, Any] = {
        "class": f"{cls.__module__}.{cls.__qualname__}",
        "attrs": {name: encode(attrs[name]) for name in sorted(attrs)},
    }
    if not attrs:
        # no attributes and no repr: compare by type only, and say so
        payload["opaque"] = True
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

    def body(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "entry": self.entry,
            "inputs": encode(self.inputs),
            "outcome": self.outcome,
            "value": encode(self.value) if self.outcome == "return" else None,
            "exception": self.exception,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "controls": list(self.controls),
        }

    def fingerprint_body(self) -> dict[str, Any]:
        """The observation WITHOUT the control list.

        `controls` is metadata about the run, not behaviour: a shimmed and an
        unshimmed pass must be comparable, so the comparison hash excludes it
        while the stored blob keeps it.
        """
        body = self.body()
        body.pop("controls", None)
        return body

    @property
    def sha256(self) -> str:
        """Identity of the stored blob (includes controls)."""
        return hashlib.sha256(
            json.dumps(self.body(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @property
    def fingerprint(self) -> str:
        """Identity of the BEHAVIOUR (excludes controls)."""
        text = json.dumps(self.fingerprint_body(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def to_json(self) -> str:
        return json.dumps(self.body(), sort_keys=True, separators=(",", ":"))


@dataclass
class BlobStore:
    """Content-addressed observations: `<root>/<sha256[:2]>/<sha256>.json`."""

    root: Path
    written: set[str] = field(default_factory=set)

    def put(self, observation: Observation) -> str:
        key = observation.sha256
        path = self.root / key[:2] / f"{key}.json"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(observation.to_json() + "\n", encoding="utf-8")
        self.written.add(key)
        return key
