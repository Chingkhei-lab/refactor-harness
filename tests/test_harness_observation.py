"""Canonical encoding: the bytes the whole harness compares on.

Every rule here exists because the naive version failed on a real case from the
parse probe: an opaque `datetime.date` made a changed date compare EQUAL.
"""

from __future__ import annotations

import datetime
import decimal
import enum
import pathlib
import uuid

from harness.observation import BlobStore, Observation, canonical_json, digest, encode


class Colour(enum.Enum):
    RED = 1


class Thing:
    def __init__(self) -> None:
        self.b = 2
        self.a = 1


def test_object_keys_are_sorted() -> None:
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_floats_are_tagged_so_nan_and_zero_survive() -> None:
    assert encode(float("nan")) == {"__float__": "nan"}
    assert encode(float("-inf")) == {"__float__": "-inf"}
    assert encode(0.1) == {"__float__": "0.1"}
    assert encode(-0.0) != encode(0.0)


def test_bytes_sets_tuples_are_distinct_and_ordered() -> None:
    assert encode(b"\x00\xff") == {"__bytes__": "00ff"}
    assert encode({3, 1, 2}) == {"__set__": [1, 2, 3]}
    assert encode((1, 2)) == {"__tuple__": [1, 2]}
    assert encode([1, 2]) != encode((1, 2)), "a tuple and a list are different behaviour"


def test_iterators_are_materialised() -> None:
    assert encode(iter([1, 2])) == {"__iterator__": [1, 2]}
    assert encode(x * 2 for x in (1, 2)) == {"__iterator__": [2, 4]}


def test_exception_shape_keeps_type_and_message() -> None:
    try:
        raise ValueError("bad thing")
    except ValueError as exc:
        encoded = encode(exc)
    assert encoded == {
        "__exception__": {
            "type": "ValueError",
            "module": "builtins",
            "message": "bad thing",
        }
    }


def test_scalars_without_dict_are_tagged_not_opaque() -> None:
    assert encode(datetime.date(2023, 11, 14)) == {"__date__": "2023-11-14"}
    assert encode(datetime.datetime(2023, 11, 14, 22, 13, 20)) == {
        "__datetime__": "2023-11-14T22:13:20"
    }
    assert encode(datetime.timedelta(seconds=90)) == {"__timedelta__": "90.0"}
    assert encode(decimal.Decimal("1.500")) == {"__decimal__": "1.500"}
    assert encode(uuid.UUID(int=5)) == {"__uuid__": "00000000-0000-0000-0000-000000000005"}
    assert encode(pathlib.PurePosixPath("a/b")) == {"__path__": "a/b"}
    assert encode(Colour.RED)["value"] == 1


def test_objects_compare_by_type_and_sorted_attributes() -> None:
    encoded = encode(Thing())
    assert encoded == {
        "__object__": {
            "class": f"{Thing.__module__}.Thing",
            "attrs": {"a": 1, "b": 2},
        }
    }
    assert "opaque" not in encoded["__object__"]


def test_digest_is_stable_and_value_sensitive() -> None:
    assert digest({"a": [1, 2]}) == digest({"a": [1, 2]})
    assert digest({"a": [1, 2]}) != digest({"a": [1, 3]})
    assert digest(datetime.date(2023, 11, 14)) != digest(datetime.date(2024, 11, 14))


def test_observation_fingerprint_ignores_controls(tmp_path: pathlib.Path) -> None:
    base = {"case_id": "c1", "entry": "parse", "inputs": {"args": []}, "outcome": "return"}
    shimmed = Observation(**base, value=1, controls=("clock_frozen",))
    bare = Observation(**base, value=1, controls=())
    assert shimmed.fingerprint == bare.fingerprint, "controls are metadata, not behaviour"
    assert shimmed.sha256 != bare.sha256, "the stored blob still records them"


def test_blob_store_is_content_addressed(tmp_path: pathlib.Path) -> None:
    store = BlobStore(tmp_path)
    observation = Observation(
        case_id="c1", entry="parse", inputs={"args": ["{}"]}, outcome="return", value="x"
    )
    key = store.put(observation)
    path = tmp_path / key[:2] / f"{key}.json"
    assert path.exists()
    assert store.put(observation) == key, "same observation, same address"


def test_infinite_iterator_is_truncated_not_hung() -> None:
    """parse.findall('{:tt}', '22:13:20') never terminates.

    Materialising a lazy value is unbounded work chosen by the target, so the
    harness bounds it and records the truncation. Without this the first
    baseline hung for 900 s on that one case.
    """

    def endless() -> object:
        while True:
            yield 1

    encoded = encode(endless())
    assert len(encoded["__iterator__"]) == 1000
    assert encoded["truncated"] is True
    assert encoded["limit"] == 1000


def test_finite_iterator_is_not_marked_truncated() -> None:
    assert "truncated" not in encode(iter([1, 2, 3]))
