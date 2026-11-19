"""Case sets for a target: a JSON spec of entry points plus example inputs.

For `parse` (stage 1) the case set mixes four sources, and every case records
which one it came from so a report can say what it is actually measuring:

    docs        the library's own documented examples (inputs only — the golden
                record is what the OLD VERSION did, never the docstring's claim)
    grammar     format strings built from the field grammar (`{}`, `{name}`,
                type specs, width/precision) against generated strings
    malformed   formats that are supposed to fail: unclosed braces, unknown
                type characters, bad nesting — these exercise exception type
                AND message, which is where 'equivalent' usually leaks
    hypothesis  `st.text()` inputs collected under `derandomize=True`, so the
                set is reproducible; the strings are fed to well-formed formats

Nothing here asserts an expected value. A case is an input; the observation is
whatever the code under test actually did.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CASE_SET_VERSION = 1

# Documented example inputs (not expected outputs).
_DOC_EXAMPLES: tuple[tuple[str, str], ...] = (
    ("{}", "hello"),
    ("{}", "42"),
    ("Hi {}", "Hi Alice"),
    ("{name}", "bob"),
    ("{a} {b}", "one two"),
    ("{:d}", "12345"),
    ("{:w}", "hello world"),
    ("{:S}", "ABC"),
    ("{}", ""),
    ("{:d}", "notdigits"),
    ("{}", "  padded  "),
    ("{a}-{b}", "x-y"),
    ("{:.2f}", "3.14"),
    ("{:>5}", "ab"),
)

# The field grammar: (format, sample input that should match it).
_GRAMMAR: tuple[tuple[str, str], ...] = (
    ("{}", "abc"),
    ("{}", "123"),
    ("{}", "-1.5"),
    ("{}", "ünïcode"),
    ("{x}", "value"),
    ("{x}{y}", "ab"),
    ("{:d}", "007"),
    ("{:d}", "-12"),
    ("{:w}", "word"),
    ("{:W}", "word"),
    ("{:l}", "letters"),
    ("{:s}", " \t spaced"),
    ("{:S}", "UPPER"),
    ("{:f}", "1.5"),
    ("{:n}", "1,000"),
    ("{:x}", "deadbeef"),
    ("{:o}", "755"),
    ("{:b}", "1010"),
    ("{:.1f}", "2.5"),
    ("{:.3f}", "1.23456"),
    ("{:5}", "ab"),
    ("{a:a}", "a"),
    # date/time types: parse builds these from strptime pieces
    ("{:ti}", "2023-11-14"),
    ("{:tg}", "14.11.2023"),
    ("{:ta}", "Tue Nov 14 2023"),
    ("{:te}", "14 Nov 2023"),
    ("{:th}", "Nov 14 2023"),
    ("{:tc}", "Tue Nov 14 22:13:20 2023"),
    ("{:tt}", "22:13:20"),
    ("{:ts}", "2023-11-14 22:13:20"),
    ("{:tl}", "Tue, 14 Nov 2023 22:13:20 +0000"),
    # NO YEAR in the format: parse fills it from datetime.today().year, so these
    # two cases are the clock probe — the shim must make them deterministic.
    ("{:%b %d}", "Nov 14"),
    ("{:%H:%M}", "22:13"),
)

# Formats that must fail, or that fail in interesting ways.
_MALFORMED: tuple[str, ...] = (
    "{",
    "}",
    "{} {",
    "{:z}",
    "{name",
    "{ }",
    "{} {} {}",
    "{0}",
    "{:d",
    "{{}}",
    "{:}",
    "{:-}",
    "{:.}",
)

_ALPHABET = "abcXYZ 0123456789-_./:"


@dataclass(frozen=True, slots=True)
class Case:
    id: str
    source: str
    entry: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any] = field(default_factory=dict)

    def body(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "entry": self.entry,
            "args": list(self.args),
            "kwargs": dict(self.kwargs),
        }

    @property
    def key(self) -> tuple[str, str]:
        return self.entry, json.dumps([self.args, self.kwargs], sort_keys=True, default=str)


def _case(source: str, index: int, entry: str, args: tuple[Any, ...], **kwargs: Any) -> Case:
    return Case(id=f"{source}-{index:04d}", source=source, entry=entry, args=args, kwargs=kwargs)


def _dedupe(cases: list[Case]) -> list[Case]:
    seen: set[tuple[str, str]] = set()
    unique: list[Case] = []
    for case in cases:
        if case.key in seen:
            continue
        seen.add(case.key)
        unique.append(case)
    return unique


def _hypothesis_strings(count: int) -> list[str]:
    """Deterministic samples: `derandomize=True` replays the same set."""
    from hypothesis import HealthCheck, given, settings
    from hypothesis import strategies as st

    collected: list[str] = []
    strategy = st.text(alphabet=_ALPHABET, max_size=14)

    @settings(
        max_examples=count,
        derandomize=True,
        database=None,
        deadline=None,
        suppress_health_check=list(HealthCheck),
    )
    @given(strategy)
    def _collect(value: str) -> None:
        collected.append(value)

    _collect()
    return collected


def build_parse_cases(hypothesis_examples: int = 40, seed: int = 1234) -> list[Case]:
    """The stage-1 case set for `parse` — inputs only, no expectations."""
    rng = random.Random(seed)  # noqa: S311 - deterministic case generation, not crypto
    cases: list[Case] = []

    for index, (pattern, text) in enumerate(_DOC_EXAMPLES):
        cases.append(_case("docs", index, "parse", (pattern, text)))
        cases.append(_case("docs", index, "search", (pattern, text)))
        cases.append(_case("docs", index, "findall", (pattern, text)))

    for index, (pattern, text) in enumerate(_GRAMMAR):
        cases.append(_case("grammar", index, "parse", (pattern, text)))
        cases.append(_case("grammar", index, "parse", (pattern, text), evaluate_result=False))
        cases.append(_case("grammar", index, "search", (pattern, text)))
        cases.append(_case("grammar", index, "findall", (pattern, text)))

    for index, pattern in enumerate(_MALFORMED):
        cases.append(_case("malformed", index, "parse", (pattern, "sample")))
        cases.append(_case("malformed", index, "search", (pattern, "sample")))

    # generated: a well-formed format, a random string
    generated_formats = ["{}", "{:d}", "{:w}", "{a}", "{:.2f}", "{:S}", "{}-{}"]
    for index, text in enumerate(_hypothesis_strings(hypothesis_examples)):
        pattern = generated_formats[rng.randrange(len(generated_formats))]
        cases.append(_case("hypothesis", index, "parse", (pattern, text)))

    # round out the grammar with random strings against each documented format
    for index, (pattern, _) in enumerate(_GRAMMAR):
        text = "".join(rng.choice(_ALPHABET) for _ in range(rng.randrange(0, 12)))
        cases.append(_case("grammar-random", index, "parse", (pattern, text)))

    unique = _dedupe(cases)
    # IDs must be unique ACROSS entry points: `docs-0000` would otherwise name
    # the parse, search and findall cases for the same example, and any map
    # keyed by case_id silently collapses them.
    return [
        Case(
            id=f"{position:04d}-{case.entry}-{case.source}",
            source=case.source,
            entry=case.entry,
            args=case.args,
            kwargs=case.kwargs,
        )
        for position, case in enumerate(unique, start=1)
    ]


def save_cases(path: Path, cases: list[Case], target: str, version: str) -> None:
    payload = {
        "case_set_version": CASE_SET_VERSION,
        "case_set_hash": case_set_hash(cases),
        "target": target,
        "version": version,
        "cases": [case.body() for case in cases],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_cases(path: Path) -> list[Case]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [
        Case(
            id=item["id"],
            source=item["source"],
            entry=item["entry"],
            args=tuple(item["args"]),
            kwargs=dict(item.get("kwargs") or {}),
        )
        for item in payload["cases"]
    ]


def case_set_hash(cases: list[Case]) -> str:
    """sha256 of the case set itself.

    `case_set_version` is a schema number, not an identity: two different sets
    can share it. The hash goes into every observation, every blob path and
    every report, and comparing two runs with different hashes is refused.
    """
    payload = json.dumps([case.body() for case in cases], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def counts_by_source(cases: list[Case]) -> dict[str, int]:
    return dict(sorted(Counter(case.source for case in cases).items()))
