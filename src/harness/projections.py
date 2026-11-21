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

import re
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


_FRAME = re.compile(r'^\s*File "(?P<path>[^"]*)", line \d+, in (?P<func>.+?)\s*$')
# The path group is non-greedy and may contain colons: a Windows path starts with
# a drive letter, and a `[^:]*` group silently fails to match there — which is how
# a warning line kept its absolute path on one platform and lost it on another.
_WARNING = re.compile(r"^(?P<path>.+?\.py):\d+: (?P<kind>\w*Warning): ")
_ECHO = re.compile(r"^\s+\S")


def _canonicalise_stream(text: str) -> str:
    """A traceback as type, message and call path — never as source layout.

    A CLI case that triggers an internal error observes a traceback, and a
    traceback carries the target's own file path, line numbers and the text of
    the raising line. None of that is behaviour: a *staged* tree at a different
    path changes it, a two-line migration changes it, and a single-site mutation
    changes it whenever the mutated line is the one printed. Left alone it made
    every mutant differ on two of the cases that print one, which reads as a
    100 % kill rate and hides the real ones.

    What survives: the exception's type and message, the order and names of the
    frames, and everything else the program wrote. What goes: absolute paths,
    line numbers, the source echo with its caret line, and CRLF. CRLF goes
    because it is the host's text translation, not the program's output — the
    same target prints `\r\n` on Windows and `\n` on Linux through a pipe, and a
    case set whose values depend on that cannot be compared across platforms.
    Only `\r\n` is folded: `str.splitlines()` would also split on form feed and
    U+2028, and silently delete them from the observed value.
    """
    normalised = text.replace("\r\n", "\n")
    out: list[str] = []
    in_traceback = False
    expect_echo = False
    for line in normalised.split("\n"):
        if line.startswith("Traceback (most recent call last):"):
            in_traceback = True
            expect_echo = False
            out.append(line)
            continue
        if in_traceback:
            frame = _FRAME.match(line)
            if frame:
                out.append(f'  File "<target>", in {frame.group("func")}')
                expect_echo = True
                continue
            if expect_echo and line.strip():
                # The echoed source line of the raising statement.
                expect_echo = False
                continue
            if set(line.strip()) <= set("^~") and line.strip():
                # The caret line under it: as long as the expression it points
                # at, so a re-wrapped call changes it even when the frame is the
                # same frame.
                continue
            if line.strip() and not line.startswith((" ", "\t")):
                in_traceback = False  # the exception line ends the block
                out.append(line)
                continue
            out.append(line)
            continue
        warning = _WARNING.match(line)
        if warning:
            name = warning.group("path").replace("\\", "/").rsplit("/", 1)[-1]
            out.append(f"{name}: {warning.group('kind')}: " + line[warning.end():])
            continue
        out.append(line)
    return "\n".join(out)

def canonicalise_stream(value: Any) -> Any:
    """Projection for a CLI observation's `stdout`/`stderr` fields."""
    return _canonicalise_stream(value) if isinstance(value, str) else value


PROJECTIONS: dict[str, Callable[[Any], Any]] = {
    "identity": lambda value: value,
    "cli_stream": canonicalise_stream,
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
