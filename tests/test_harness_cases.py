"""Case sets: unique IDs, honest provenance, and the clock probe.

The ID collision these tests pin was a real defect: `docs-0000` named the
parse, search AND findall case for one example, so any map keyed by case_id
collapsed 273 cases into ~90 and the shim-dependence report read zero.
"""

from __future__ import annotations

import pathlib

from harness.cases import build_parse_cases, counts_by_source, load_cases, save_cases


def test_ids_are_unique_across_entry_points() -> None:
    cases = build_parse_cases(generated_examples=5)
    ids = [case.id for case in cases]
    assert len(ids) == len(set(ids))


def test_every_case_declares_its_source() -> None:
    cases = build_parse_cases(generated_examples=5)
    counts = counts_by_source(cases)
    assert counts["docs"] and counts["grammar"] and counts["malformed"]
    assert counts["hypothesis"] >= 5
    assert sum(counts.values()) == len(cases)


def test_malformed_formats_are_present() -> None:
    cases = build_parse_cases(generated_examples=5)
    patterns = {case.args[0] for case in cases if case.args}
    assert "{" in patterns and "{:z}" in patterns and "{0}" in patterns


def test_clock_probe_cases_exist() -> None:
    """Formats with no year force parse to read datetime.today().year.

    These are the cases the determinism shim must decide; the baseline report
    asserts that exactly these (and only these) change without the shim.
    """
    cases = build_parse_cases(generated_examples=5)
    patterns = {case.args[0] for case in cases if case.args}
    assert "{:%b %d}" in patterns


def test_cases_round_trip_through_json(tmp_path: pathlib.Path) -> None:
    cases = build_parse_cases(generated_examples=5)
    path = tmp_path / "parse.json"
    save_cases(path, cases, "parse", "1.22.4")
    loaded = load_cases(path)
    assert [case.body() for case in loaded] == [case.body() for case in cases]


def test_the_parse_case_set_is_reproducible_from_code() -> None:
    """The committed stage-1 case set must be the one the generator produces.

    It was not. The 40 `hypothesis` inputs were collected at build time and the
    collection was not reproducible: rebuilding produced `283155b0`, `ed283159`,
    then `49e73e45` from the same library version, and the baseline script
    overwrote the committed file each time. A case set whose identity changes
    when it is rebuilt cannot be what a report's hash refers to, so the sample is
    frozen as data, the file is verified instead of rewritten, and this test is
    what makes a silent drift fail here rather than in a report.
    """
    import json

    from harness.cases import build_parse_cases, case_set_hash

    path = pathlib.Path(__file__).resolve().parents[1] / "harness" / "cases" / "parse.json"
    committed = json.loads(path.read_text(encoding="utf-8"))
    cases = build_parse_cases()
    assert len(cases) == 273
    assert committed["case_set_hash"] == case_set_hash(cases)
    # Regenerating twice in one process must not move the hash either.
    assert case_set_hash(build_parse_cases()) == case_set_hash(cases)
