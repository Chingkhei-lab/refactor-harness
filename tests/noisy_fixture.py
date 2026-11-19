"""A deliberately noisy library, used only by the harness tests.

`parse` is deterministic under the shim, so it cannot show whether the
normalizer works: with nothing noisy, a normalizer that does nothing passes
every test. This fixture is the opposite — five functions, each noisy in a
different way, so each control and each rule type can be shown to matter:

    noisy_uuid()       a fresh uuid4 each call
    noisy_clock()      datetime.now().isoformat()
    noisy_float()      0.1 + 0.2
    noisy_set()        list(set(...)) — iteration order
    noisy_record()     a dict with an id, a timestamp and a real payload field

The last one is the point: masking `created` and `id` must not mask `payload`,
so a change to `payload` is still reported as different.

The tests import this file as a target module and, for the "two versions"
rows, copy it with a small edit (`tests/test_harness_normalizer.py` builds
`noisy_fixture_v2` that way). Keep it import-free of the harness.
"""

from __future__ import annotations

import datetime as _dt
import uuid
from typing import Any


def noisy_uuid() -> str:
    return str(uuid.uuid4())


def noisy_clock() -> str:
    return _dt.datetime.now().isoformat()


def noisy_float() -> float:
    return 0.1 + 0.2


def noisy_set() -> list[str]:
    return list({"alpha", "beta", "gamma", "delta", "epsilon"})


def noisy_record(payload: str = "unchanged") -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "created": _dt.datetime.now().isoformat(),
        "payload": payload,
    }
