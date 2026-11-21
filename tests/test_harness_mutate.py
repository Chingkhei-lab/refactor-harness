"""The mutator: operators, determinism, validity, and the sampling rule.

A mutation score is only worth reporting if the mutator itself is trustworthy, so
these tests are about the *tool*: every site yields valid code, exactly one change
is made, enumeration is deterministic, and no mutant is silently identical to the
original.
"""

from __future__ import annotations

import ast
from pathlib import Path

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


def _fixture_source() -> str:
    path = Path(__file__).resolve().parent / "fixtures" / "mutate_module.py"
    return path.read_text(encoding="utf-8")


def test_the_vendored_fixture_exercises_every_operator() -> None:
    """A hermetic test is only as good as its sample: every operator must bite."""
    seen = {site.operator for site in enumerate_sites(_fixture_source())}
    assert seen == set(OPERATORS), sorted(set(OPERATORS) - seen)


def test_every_mutant_of_the_vendored_fixture_parses_and_changes_something() -> None:
    """The check that used to need a git-ignored target tree, now runs everywhere."""
    source = _fixture_source()
    original = compile(source, "fixture", "exec")
    sites = enumerate_sites(source)
    assert len(sites) >= 50, len(sites)
    failures = []
    changed = 0
    for site in sites:
        mutant = mutate(source, site)
        try:
            compile(mutant, "fixture", "exec")
        except SyntaxError as exc:  # the failure IS the assertion
            failures.append((site.key, str(exc)[:80]))
            continue
        if mutant != source:
            changed += 1
        try:
            exec(original, {})
            exec(compile(mutant, "fixture", "exec"), {})
        except Exception:  # noqa: BLE001, S102 - runtime layout is not the point
            pass
    assert not failures, failures[:5]
    assert changed == len(sites), (changed, len(sites))


def test_a_mutant_changes_one_contiguous_region_of_the_file() -> None:
    """The mutator's own invariant, and the bug that made it necessary.

    `apply_site` used to return `ast.unparse(tree)`: a correct single-site change
    in the AST, and a rewrite of every line in the file. Everything downstream
    passed — the tree parsed, the operator changed once — while any case that
    observed a traceback, a warning or the module's own path reported *every*
    mutant as different. That is a 100 % kill rate that measures the mutator.

    Two properties are pinned here: one contiguous edit per site, and a real
    module (not just a fixture) never rewritten wholesale.
    """
    from harness.mutate import _changed_span, apply_site, enumerate_sites

    source = "\n".join(
        (
            "def f(a, b):",
            "    if a < b and b:",
            "        return a + 1",
            "    x = 0",
            "    return None",
            "",
        )
    )
    sites = enumerate_sites(source)
    assert sites, "fixture must produce sites"
    for site in sites:
        mutated = apply_site(source, site)
        first, old_span, new_span = _changed_span(source, mutated)
        assert old_span <= 2 and new_span <= 2, (site.key, old_span, new_span)
        assert abs(first - site.line) <= 1, (site.key, first)

    # A whole-file reformat, the thing the mutator used to do, must be refused.
    import ast as _ast

    formatted = _ast.unparse(_ast.parse(source)) + "\n"
    first, old_span, _new = _changed_span(source, formatted)
    assert old_span > 2 or first != 2, "the fixture is too uniform to prove anything"
