"""`run_verify`, the hidden gate, and the budgets that make them ungameable.

The three limits are the review's, and each is here as a test rather than a
comment: at most five differing cases of 500 characters, eight `run_verify` calls
per run, two `finish` attempts with a category-only answer.
"""

from __future__ import annotations

import pytest

from agent.verify import (
    GATE_CASE_TIMEOUT,
    GATE_DIFFERENT_OUTSIDE_PATTERNS,
    GATE_INFRASTRUCTURE,
    GATE_PASS,
    GATE_SUITE_FAILURE,
    MAX_DIFF_CHARS,
    MAX_DIFFERING_CASES,
    MAX_FINISH_ATTEMPTS,
    MAX_VERIFY_CALLS,
    CaseDifference,
    FinishAttemptsExhausted,
    GateResult,
    VerifyBudgetExhausted,
    VerifyResult,
    VerifySession,
    classify_gate,
    differences_outside,
    truncate_diff,
)


class StubVerifier:
    def __init__(self, result: VerifyResult) -> None:
        self.result = result
        self.calls = 0

    def run(self, *, differences: int = MAX_DIFFERING_CASES) -> VerifyResult:
        self.calls += 1
        return self.result


class StubGate:
    def __init__(self, result: GateResult) -> None:
        self.result = result
        self.calls = 0

    def evaluate(self, *, allowed_patterns: object = ()) -> GateResult:
        self.calls += 1
        return self.result


def difference(index: int) -> CaseDifference:
    return CaseDifference(
        case_id=f"fixture-{index}",
        entry="pycodestyle.py",
        path="$.value.stderr",
        left="x" * 900,
        right="y" * 900,
    )


def test_a_verify_result_shows_at_most_five_cases_and_500_characters() -> None:
    differences = tuple(difference(index) for index in range(9))
    result = VerifyResult(
        identical=10, different=9, approved=0, unsupported=0, differences=differences[:5]
    )
    rendered = result.render()

    assert rendered.count("- fixture-") == MAX_DIFFERING_CASES
    assert "showing 5 of 9 differing cases" in rendered
    for shown in differences[:5]:
        body = shown.as_dict()
        assert len(body["left"]) <= MAX_DIFF_CHARS + 40  # marker + count
        assert body["left"].endswith("chars]")


def test_the_golden_blob_is_never_handed_to_the_model() -> None:
    """A difference carries a leaf, and the leaf is cut; no full value, no files."""
    rendered = VerifyResult(
        identical=0, different=1, approved=0, unsupported=0, differences=(difference(1),)
    ).render()
    assert "files" not in rendered
    assert "case_set_hash" not in rendered
    assert len(rendered) < 2_000


def test_verify_is_capped_at_eight_calls() -> None:
    session = VerifySession(verifier=StubVerifier(VerifyResult(1, 0, 0, 0)))
    for _ in range(MAX_VERIFY_CALLS):
        session.run_verify()
    with pytest.raises(VerifyBudgetExhausted) as error:
        session.run_verify()
    assert error.value.cap == MAX_VERIFY_CALLS
    assert error.value.used == MAX_VERIFY_CALLS


def test_finish_is_capped_at_two_attempts_with_a_category_only() -> None:
    gate = StubGate(GateResult(False, GATE_DIFFERENT_OUTSIDE_PATTERNS))
    session = VerifySession(gate=gate)
    first = session.finish()
    second = session.finish()
    assert not first.passed and first.category == GATE_DIFFERENT_OUTSIDE_PATTERNS
    assert not second.passed
    with pytest.raises(FinishAttemptsExhausted) as error:
        session.finish()
    assert error.value.attempts == MAX_FINISH_ATTEMPTS
    assert gate.calls == MAX_FINISH_ATTEMPTS, "the refused attempt must not reach the gate"


def test_the_gate_answer_carries_no_case_details() -> None:
    rendered = GateResult(
        False, GATE_DIFFERENT_OUTSIDE_PATTERNS, "3 difference(s) outside the allowed patterns"
    ).render()
    assert rendered.startswith("hidden gate: fail")
    assert "fixture-" not in rendered
    assert "$.value" not in rendered


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"infrastructure_errors": 1}, GATE_INFRASTRUCTURE),
        ({"timeouts": 1}, GATE_CASE_TIMEOUT),
        ({"differences_outside_patterns": 1}, GATE_DIFFERENT_OUTSIDE_PATTERNS),
        ({"suite_failures": 2}, GATE_SUITE_FAILURE),
        ({}, GATE_PASS),
    ],
)
def test_the_decision_table_prefers_the_worst_answer(
    kwargs: dict[str, int], expected: str
) -> None:
    result = classify_gate(
        differences_outside_patterns=kwargs.get("differences_outside_patterns", 0),
        suite_failures=kwargs.get("suite_failures", 0),
        timeouts=kwargs.get("timeouts", 0),
        infrastructure_errors=kwargs.get("infrastructure_errors", 0),
    )
    assert result.category == expected
    assert result.passed is (expected == GATE_PASS)


def test_a_migration_may_change_help_text_and_exit_status_only() -> None:
    """Patterns are the migration's allowance, not a blanket excuse."""
    differences = [
        {"case_id": "probe-help", "path": "$.value.stdout"},
        {"case_id": "probe-bad", "path": "$.value.stderr"},
        {"case_id": "probe-exit", "path": "$.value.exit"},
        {"case_id": "fixture-e25", "path": "$.value.files['x.py'].sha256"},
        {"case_id": "fixture-e25", "path": "$.value.stdout_sha256"},
    ]
    outside = differences_outside(differences, ("$.value.stdout", "$.value.stderr", "$.value.exit"))
    assert {item["path"] for item in outside} == {
        "$.value.files['x.py'].sha256",
        "$.value.stdout_sha256",
    }, "a file changing is the thing under test, and must not be covered by a prefix"


def test_patterns_match_whole_segments_not_prefixes() -> None:
    assert differences_outside([{"path": "$.value.exit_code"}], ["$.value.exit"]) != []
    assert differences_outside([{"path": "$.value.exit"}], ["$.value.exit"]) == []


def test_truncation_says_how_much_it_cut() -> None:
    short = "abc"
    assert truncate_diff(short) == short
    long = "z" * (MAX_DIFF_CHARS + 120)
    cut = truncate_diff(long)
    assert cut.startswith("z" * MAX_DIFF_CHARS)
    assert "truncated: 120 chars" in cut


def test_a_verify_on_an_unchanged_candidate_reports_no_changes_not_counts() -> None:
    """The most expensive reading error in the pilot, fixed at the tool.

    Five of six pilot runs called `run_verify` on a tree they had not touched and were
    told `identical=34 different=0`. Every one of them then finished without patching,
    because a model reading counts takes zero differences as "done". The tool now
    refuses to report counts about nothing.
    """
    from agent.verify import (
        NO_CHANGES_MESSAGE,
        STATE_NO_CHANGES,
        STATE_VERIFIED,
        VerifyResult,
    )

    unchanged = VerifyResult(
        identical=0, different=0, approved=0, unsupported=0, state=STATE_NO_CHANGES
    )
    rendered = unchanged.render()
    assert rendered == NO_CHANGES_MESSAGE
    assert "identical=" not in rendered
    assert "apply_patch" in rendered
    assert "give_up" in rendered
    assert not unchanged.verified

    changed = VerifyResult(
        identical=33,
        different=1,
        approved=0,
        unsupported=0,
        state=STATE_VERIFIED,
        changed_files=("mccabe.py",),
    )
    text = changed.render()
    assert changed.verified
    # Every result says what is being compared, and to what.
    assert text.startswith("candidate vs original:")
    assert "changed: mccabe.py" in text
