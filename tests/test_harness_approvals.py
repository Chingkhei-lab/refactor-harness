"""Approvals: bound to the diff, never to the case alone.

The property under test is the one that makes an approval useful instead of a
blanket waiver: it stops applying the moment the difference it was written for
changes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.approvals import (
    ApprovalError,
    apply_approvals,
    diff_digest,
    load_approvals,
)

BASE = {
    "schema": "harness-approvals/1",
    "target": "demo 1.0",
    "approvals": [],
}


def _write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "approvals.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _entry(path: str, left: str, right: str, **over: str) -> dict:
    entry = {
        "case_id": "0001-help",
        "path": path,
        "reason": "argparse renders usage differently; accepted",
        "reviewer": "reviewer",
        "date": "2026-11-20",
        "sha256": diff_digest(path, left, right),
    }
    entry.update(over)
    return entry


def test_an_approval_is_bound_to_the_diff_not_the_case(tmp_path: Path) -> None:
    approval = _entry("$.value.stdout", "usage: old", "usage: new")
    approvals = load_approvals(_write(tmp_path, {**BASE, "approvals": [approval]}))
    same = [{"case_id": "0001-help", "path": "$.value.stdout",
             "left": "usage: old", "right": "usage: new"}]
    applied = apply_approvals(approvals, same)
    assert len(applied.approved) == 1
    assert not applied.stale


def test_a_changed_diff_makes_the_approval_stale_and_stays_different(
    tmp_path: Path,
) -> None:
    approval = _entry("$.value.stdout", "usage: old", "usage: new")
    approvals = load_approvals(_write(tmp_path, {**BASE, "approvals": [approval]}))
    changed = [{"case_id": "0001-help", "path": "$.value.stdout",
                "left": "usage: old", "right": "usage: NEWER"}]
    applied = apply_approvals(approvals, changed)
    assert not applied.approved, applied
    assert len(applied.stale) == 1
    assert applied.stale[0]["expected"] != applied.stale[0]["observed"]


def test_a_difference_without_an_approval_is_unmatched(tmp_path: Path) -> None:
    approvals = load_approvals(_write(tmp_path, BASE))
    applied = apply_approvals(
        approvals,
        [{"case_id": "0002-out", "path": "$.value.exit", "left": 0, "right": 1}],
    )
    assert not applied.approved
    assert applied.unmatched


def test_an_approval_that_matches_nothing_is_reported(tmp_path: Path) -> None:
    approval = _entry("$.value.stdout", "a", "b")
    approvals = load_approvals(_write(tmp_path, {**BASE, "approvals": [approval]}))
    applied = apply_approvals(approvals, [])
    assert applied.unmatched[0]["note"].startswith("approval matched no")


@pytest.mark.parametrize(
    "broken",
    [
        {**BASE, "schema": "something-else"},
        {**BASE, "target": ""},
        {**BASE, "approvals": [_entry("$.x", "a", "b", reason="ok")]},
        {**BASE, "approvals": [_entry("$.x", "a", "b", sha256="deadbeef")]},
        {**BASE, "approvals": [_entry("x", "a", "b")]},
        {**BASE, "approvals": [_entry("$.x", "a", "b"), _entry("$.x", "c", "d")]},
        {**BASE, "approvals": [{**_entry("$.x", "a", "b"), "reviewer": ""}]},
    ],
)
def test_a_malformed_approvals_file_is_refused(tmp_path: Path, broken: dict) -> None:
    with pytest.raises(ApprovalError):
        load_approvals(_write(tmp_path, broken))


def test_the_shipped_approvals_cover_parser_text_and_the_api_surface() -> None:
    """The shipped approvals are the migration's accepted differences, case by case.

    Two things must stay true as this file evolves: every approval names a case
    and a path, and the case whose output was *unintentionally* changed during
    the migration (`--version`, which gained a program-name prefix, was caught by
    the case set and fixed) is not in here. Approving that would have turned a
    bug into policy.
    """
    path = Path(__file__).resolve().parents[1] / "harness" / "approvals" / "pycodestyle.json"
    approvals = load_approvals(path)
    assert approvals.approvals, "the shipped file is the migration's accepted diffs"
    assert len({item.case_id for item in approvals.approvals}) == len(approvals.approvals)
    assert all(len(item.reason) >= 12 for item in approvals.approvals)
    assert "probe-version" not in {item.case_id for item in approvals.approvals}
    assert all(item.path.startswith("$.value.") for item in approvals.approvals)
    # The two api approvals are the ones that make the suite-only finding visible
    # in the harness report: `get_parser()` returns a different parser type and no
    # longer has `add_option`. They must exist and must say so, not just be
    # "approved" with a shrug.
    api = [item for item in approvals.approvals if item.case_id.startswith("api-")]
    assert {item.case_id for item in api} == {"api-module", "api-parser"}
    assert all("add_option" in item.reason or "ArgumentParser" in item.reason for item in api)
