"""Allowlisted projections: a named, reviewed view of a value the harness cannot encode.

`parse(..., evaluate_result=False)` returns a `Result` holding a `re.Match` and a
`Parser` whose fields include a `re.Pattern`. Those are opaque (no `__dict__`, no
stable identity), so the harness marks the case unsupported — 23 of 273 at
checkpoint 11, all of them in that one API path.

The fix is not a cleverer encoder. It is a **declared projection**: the case says
"observe this view of the value", the projection has a name, and the name is in an
allowlist in the harness repo. A case spec cannot run arbitrary code; it can only
select one of these.

The projection is part of the case body, so it is part of the case-set hash: a
set with projections is not the set without them, and the two cannot be compared.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class UnknownProjection(KeyError):
    """A case asked for a projection that is not in the allowlist."""


def _evaluate_result(value: Any) -> Any:
    """Everything `parse` offers, minus the compiled regex it carries around.

    `Result.evaluate_result()` returns the evaluated named/fixed fields; for a
    `ResultIterator` the whole stream is evaluated. Applied to anything that does
    not have the method, it is the identity — a projection must never fail a case
    just because the old version did not have the attribute; that difference is
    the comparison's job.
    """
    if hasattr(value, "evaluate_result"):
        return value.evaluate_result()
    if isinstance(value, (list, tuple)):
        return [_evaluate_result(item) for item in value]
    return value


def _result_fields(value: Any) -> Any:
    """Only `fixed`/`named`: a narrower view than `evaluate_result`."""
    if hasattr(value, "fixed") and hasattr(value, "named"):
        return {"fixed": list(value.fixed), "named": dict(value.named)}
    return value


PROJECTIONS: dict[str, Callable[[Any], Any]] = {
    "identity": lambda value: value,
    "evaluate_result": _evaluate_result,
    "result_fields": _result_fields,
}


def names() -> tuple[str, ...]:
    return tuple(sorted(PROJECTIONS))


def get(name: str) -> Callable[[Any], Any]:
    try:
        return PROJECTIONS[name]
    except KeyError:
        raise UnknownProjection(
            f"{name!r} is not a declared projection; allowed: {', '.join(names())}"
        ) from None
