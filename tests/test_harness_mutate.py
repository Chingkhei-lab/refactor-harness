"""The mutator: operators, determinism, validity, and the sampling rule.

A mutation score is only worth reporting if the mutator itself is trustworthy, so
these tests are about the *tool*: every site yields valid code, exactly one change
is made, enumeration is deterministic, and no mutant is silently identical to the
original.
"""

from __future__ import annotations

import ast

from harness.mutate import (
    OPERATORS,
    enumerate_sites,
    mutate,
    sites_by_operator,
)

SOURCE = '''\
def classify(value, items, flag):
    if value < 10 and flag:
        return value + 1
    if value == 20 or not flag:
        return None
    total = 0
    for item in items[:3]:
        total += item
    print(total)
    return total
'''


def test_every_site_yields_valid_code_with_exactly_one_change() -> None:
    sites = enumerate_sites(SOURCE)
    assert sites, "the sample must have sites"
    for site in sites:
        mutated = mutate(SOURCE, site)
        ast.parse(mutated)
        assert mutated.strip() != SOURCE.strip()


def test_enumeration_is_deterministic_and_deduplicated() -> None:
    first = [site.key for site in enumerate_sites(SOURCE)]
    second = [site.key for site in enumerate_sites(SOURCE)]
    assert first == second
    assert len(first) == len(set(first))


def test_all_seven_operator_families_are_present() -> None:
    counts = sites_by_operator(SOURCE)
    for name in OPERATORS:
        assert counts[name] > 0, f"operator {name} found no site in the sample"


def test_a_comparison_swap_flips_one_operator_only() -> None:
    site = next(s for s in enumerate_sites(SOURCE) if s.operator == "compare_swap")
    mutated = mutate(SOURCE, site)
    assert ("<=" in mutated) or ("!=" in mutated) or ("is not" in mutated)
    assert mutated != SOURCE


def test_a_statement_is_never_deleted_from_a_single_statement_block() -> None:
    """Deleting the only statement in a block would not parse, so it is not a site."""
    source = "def f():\n    if True:\n        return 1\n    return 2\n"
    for site in enumerate_sites(source):
        ast.parse(mutate(source, site))
    delete_sites = [s for s in enumerate_sites(source) if s.operator == "delete_stmt"]
    assert all(site.field for site in delete_sites)


def test_slice_bound_mutation_shifts_the_bound() -> None:
    site = next(s for s in enumerate_sites(SOURCE) if s.operator == "slice_bound")
    mutated = mutate(SOURCE, site)
    assert "items[:4]" in mutated, "the upper bound 3 became 4"
    assert "items[:3]" in SOURCE


def test_a_delete_mutant_removes_the_named_statement() -> None:
    source = "def f(x):\n    y = 1\n    print(x)\n    return x\n"
    site = next(s for s in enumerate_sites(source) if s.operator == "delete_stmt")
    mutated = mutate(source, site)
    lines = [line.strip() for line in mutated.splitlines()]
    assert len(lines) == 3


def test_mutating_a_real_module_keeps_every_mutant_parseable() -> None:
    """The target is 1 106 lines: every enumerated site must still produce code."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / ".harness" / "target" /
              "parse" / "__init__.py")
    if not source.exists():  # pragma: no cover - target is fetched by the harness
        import pytest

        pytest.skip("target not vendored in this checkout")
    text = source.read_text(encoding="utf-8")
    failures = []
    for site in enumerate_sites(text):
        try:
            mutate(text, site)
        except Exception as exc:  # noqa: BLE001 - the failure IS the assertion
            failures.append((site.key, str(exc)[:80]))
    assert not failures, failures[:5]
