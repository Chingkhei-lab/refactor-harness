"""Case sets: unique IDs, honest provenance, and the clock probe.

The ID collision these tests pin was a real defect: `docs-0000` named the
parse, search AND findall case for one example, so any map keyed by case_id
collapsed 273 cases into ~90 and the shim-dependence report read zero.
"""

from __future__ import annotations

import pathlib

from harness.cases import build_parse_cases, counts_by_source, load_cases, save_cases


def test_ids_are_unique_across_entry_points() -> None:
    cases = build_parse_cases(hypothesis_examples=5)
    ids = [case.id for case in cases]
    assert len(ids) == len(set(ids))


def test_every_case_declares_its_source() -> None:
    cases = build_parse_cases(hypothesis_examples=5)
    counts = counts_by_source(cases)
    assert counts["docs"] and counts["grammar"] and counts["malformed"]
    assert counts["hypothesis"] >= 5
    assert sum(counts.values()) == len(cases)


def test_malformed_formats_are_present() -> None:
    cases = build_parse_cases(hypothesis_examples=5)
    patterns = {case.args[0] for case in cases if case.args}
    assert "{" in patterns and "{:z}" in patterns and "{0}" in patterns


def test_clock_probe_cases_exist() -> None:
    """Formats with no year force parse to read datetime.today().year.

    These are the cases the determinism shim must decide; the baseline report
    asserts that exactly these (and only these) change without the shim.
    """
    cases = build_parse_cases(hypothesis_examples=5)
    patterns = {case.args[0] for case in cases if case.args}
    assert "{:%b %d}" in patterns


def test_cases_round_trip_through_json(tmp_path: pathlib.Path) -> None:
    cases = build_parse_cases(hypothesis_examples=5)
    path = tmp_path / "parse.json"
    save_cases(path, cases, "parse", "1.22.4")
    loaded = load_cases(path)
    assert [case.body() for case in loaded] == [case.body() for case in cases]
