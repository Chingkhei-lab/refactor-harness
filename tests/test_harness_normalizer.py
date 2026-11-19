"""The normalizer's own test: a noisy fixture, one control at a time.

`parse` is deterministic under the shim, so it cannot demonstrate that the
normalizer works — with nothing noisy, a normalizer that does nothing passes.
This module builds a deliberately noisy target and shows the two layers in
order: the control removes the noise, and only when a control is switched off
does a declared rule make the result equivalent. A change outside a masked field
is still reported as different.

Run it directly to print the table (it is also the pytest suite):

    python tests/test_harness_normalizer.py
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from harness.cases import Case, case_set_hash  # noqa: E402
from harness.compare import compare, summarise  # noqa: E402
from harness.determinism import write_shim  # noqa: E402
from harness.rules import load_rules  # noqa: E402
from harness.runner import Pass, run_pass  # noqa: E402

FIXTURE = TESTS_DIR / "noisy_fixture.py"

CASES = [
    Case(id="0101-uuid-grammar", source="grammar", entry="noisy_uuid", args=()),
    Case(id="0102-clock-grammar", source="grammar", entry="noisy_clock", args=()),
    Case(id="0103-float-grammar", source="grammar", entry="noisy_float", args=()),
    Case(id="0104-set-grammar", source="grammar", entry="noisy_set", args=()),
    Case(id="0105-record-grammar", source="grammar", entry="noisy_record", args=()),
]

# The refactor under test: same API, a float computed differently, a set
# iterated in another order, and one payload value changed on purpose.
V2_PATCHES = (
    ("return 0.1 + 0.2", "return 0.1 + 0.2 + 1e-16"),
    ('return list({"alpha", "beta", "gamma", "delta", "epsilon"})',
     'return sorted({"alpha", "beta", "gamma", "delta", "epsilon"}, reverse=True)'),
    ('"payload": payload,', '"payload": "changed",'),
)


def _write_version(directory: Path, module: str, patches: tuple[tuple[str, str], ...]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    source = FIXTURE.read_text(encoding="utf-8")
    for old, new in patches:
        assert old in source, f"fixture changed, patch no longer applies: {old!r}"
        source = source.replace(old, new)
    (directory / f"{module}.py").write_text(source, encoding="utf-8")
    return directory


def _rules_file(tmp: Path, name: str, rules: list[dict[str, object]], set_hash: str) -> Path:
    path = tmp / f"rules-{name}.json"
    path.write_text(
        json.dumps(
            {"target": "noisy_fixture", "case_set_hash": set_hash, "rules": rules},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


@dataclass
class Scenario:
    name: str
    disabled: tuple[str, ...]
    patches: tuple[tuple[str, str], ...]
    rules: list[dict[str, object]]
    without_rules: dict[str, int]
    with_rules: dict[str, int]


def _passes(tmp: Path, module: str, directory: Path, disabled: tuple[str, ...]) -> list[Pass]:
    shim = write_shim(tmp / "shim")
    return [
        run_pass(
            python=sys.executable,
            checkout=directory,
            shim_dir=shim,
            src_dir=REPO_ROOT / "src",
            target_paths=[directory],
            cases=CASES,
            index=index,
            module=module,
            workdir=tmp / f"work-{module}-{index}",
            shim_disable=disabled,
            expected_case_set_hash=case_set_hash(CASES),
        )
        for index in (1, 2)
    ]


def _counts(left: Pass, right: Pass, rules_path: Path | None) -> dict[str, int]:
    rules = load_rules(rules_path, case_set_hash=case_set_hash(CASES)) if rules_path else None
    verdicts = [
        compare(left.by_case()[case.id], right.by_case()[case.id], rules) for case in CASES
    ]
    summary = summarise(verdicts)
    summary.pop("undecidable_reasons", None)
    summary["different"] = sum(1 for verdict in verdicts if verdict.report_class == "different")
    return summary


def run_scenarios(tmp: Path) -> list[Scenario]:
    """Every row of the table: a control off, or a refactor, with and without rules."""
    v1 = _write_version(tmp / "v1", "noisy_fixture_v1", ())
    v2 = _write_version(tmp / "v2", "noisy_fixture_v2", V2_PATCHES)
    set_hash = case_set_hash(CASES)

    uuid_rules = [
        {
            "id": "uuid-value",
            "path": "$.value",
            "type": "uuid",
            "reason": "A/A: noisy_uuid returns a fresh uuid4 unless uuid_frozen applies.",
        },
        {
            "id": "uuid-record-id",
            "path": "$.value.id",
            "type": "uuid",
            "reason": "A/A: noisy_record.id is a uuid4 for the same reason.",
        },
    ]
    timestamp_rules = [
        {
            "id": "clock-value",
            "path": "$.value",
            "type": "timestamp",
            "reason": "A/A: noisy_clock reads the wall clock when clock_frozen is off.",
            "tolerance_seconds": 86400,
        },
        {
            "id": "clock-record-created",
            "path": "$.value.created",
            "type": "timestamp",
            "reason": "A/A: noisy_record.created reads the wall clock too.",
            "tolerance_seconds": 86400,
        },
    ]
    float_rules = [
        {
            "id": "float-sum",
            "path": "$.value",
            "type": "float",
            "reason": "refactor: 0.1 + 0.2 + 1e-16 is a rounding difference, not behaviour.",
            "tolerance": 1e-9,
        }
    ]
    set_rules = [
        {
            "id": "set-order",
            "path": "$.value",
            "type": "unordered_list",
            "reason": "refactor: the same set iterated in another order.",
        }
    ]

    plan = [
        ("controls on (same tree)", (), (), []),
        ("uuid_frozen off", ("uuid_frozen",), (), uuid_rules),
        ("clock_frozen off", ("clock_frozen",), (), timestamp_rules),
        ("refactor: float rounding", (), V2_PATCHES[:1], float_rules),
        ("refactor: set order", (), V2_PATCHES[:2], set_rules),
        ("refactor: payload changed", (), V2_PATCHES, uuid_rules + timestamp_rules),
    ]

    scenarios: list[Scenario] = []
    for index, (name, disabled, patches, rules) in enumerate(plan):
        module = "noisy_fixture_v2" if patches else "noisy_fixture_v1"
        directory = v2 if patches else v1
        if patches == V2_PATCHES[:1]:
            directory = _write_version(tmp / "v2-float", "noisy_fixture_v2", V2_PATCHES[:1])
        elif patches == V2_PATCHES[:2]:
            directory = _write_version(tmp / "v2-set", "noisy_fixture_v2", V2_PATCHES[:2])
        old_passes = _passes(tmp / f"s{index}-old", "noisy_fixture_v1", v1, disabled)
        new_passes = _passes(tmp / f"s{index}-new", module, directory, disabled)
        rules_path = _rules_file(tmp, f"s{index}", rules, set_hash) if rules else None
        scenarios.append(
            Scenario(
                name=name,
                disabled=disabled,
                patches=patches,
                rules=rules,
                without_rules=_counts(old_passes[0], new_passes[0], None),
                with_rules=_counts(old_passes[0], new_passes[0], rules_path),
            )
        )
    return scenarios


def _table(scenarios: list[Scenario]) -> str:
    lines = [
        "| Scenario | Control off | Classes without rules | Classes with rules |",
        "| --- | --- | --- | --- |",
    ]
    for scenario in scenarios:
        control = ", ".join(scenario.disabled) or "—"
        without = ", ".join(f"{k} {v}" for k, v in scenario.without_rules.items() if v)
        with_rules = ", ".join(f"{k} {v}" for k, v in scenario.with_rules.items() if v)
        lines.append(f"| {scenario.name} | {control} | {without} | {with_rules} |")
    return chr(10).join(lines)


def test_controls_on_need_no_rules(tmp_path: Path) -> None:
    scenarios = run_scenarios(tmp_path)
    first = scenarios[0]
    assert first.without_rules["identical"] == len(CASES)
    assert not first.rules


def test_a_disabled_control_is_noise_that_a_rule_absorbs(tmp_path: Path) -> None:
    scenarios = run_scenarios(tmp_path)
    uuid_row = scenarios[1]
    assert uuid_row.without_rules["different"] >= 2, "noise must be visible first"
    assert uuid_row.with_rules["identical"] + uuid_row.with_rules["equivalent"] == len(CASES)
    assert uuid_row.with_rules["equivalent"] >= 2, "the rule must have been applied"

    clock_row = scenarios[2]
    assert clock_row.without_rules["different"] >= 2
    assert clock_row.with_rules["equivalent"] >= 2


def test_a_change_outside_the_masked_field_is_still_different(tmp_path: Path) -> None:
    """The record's id and created are masked; its payload is not."""
    scenarios = run_scenarios(tmp_path)
    payload_row = scenarios[5]
    assert payload_row.with_rules["different"] == 3, (
        "payload, float and set must all survive the rules"
    )

    # and the reported path names the payload, not the masked fields
    v1 = _write_version(tmp_path / "pv1", "noisy_fixture_v1", ())
    v2 = _write_version(tmp_path / "pv2", "noisy_fixture_v2", V2_PATCHES)
    old = _passes(tmp_path / "po", "noisy_fixture_v1", v1, ())[0]
    new = _passes(tmp_path / "pn", "noisy_fixture_v2", v2, ())[0]
    rules = load_rules(
        _rules_file(
            tmp_path,
            "record",
            [
                {"id": "rec-id", "path": "$.value.id", "type": "uuid",
                 "reason": "A/A: uuid4 per call in noisy_record"},
                {"id": "rec-created", "path": "$.value.created", "type": "timestamp",
                 "reason": "A/A: wall clock in noisy_record", "tolerance_seconds": 86400},
            ],
            case_set_hash(CASES),
        ),
        case_set_hash=case_set_hash(CASES),
    )
    verdict = compare(
        old.by_case()["0105-record-grammar"], new.by_case()["0105-record-grammar"], rules
    )
    assert verdict.report_class == "different"
    assert verdict.path == "$.value.payload", verdict.path
    assert len(verdict.masked) == 2


def test_float_and_set_rules_are_narrow(tmp_path: Path) -> None:
    scenarios = run_scenarios(tmp_path)
    float_row, set_row = scenarios[3], scenarios[4]
    assert float_row.without_rules["different"] == 1
    assert float_row.with_rules["equivalent"] == 1
    assert set_row.without_rules["different"] == 2, "float and set both differ here"
    assert set_row.with_rules["equivalent"] == 1, "only the set rule applies"
    assert set_row.with_rules["different"] == 1, "the float difference must remain"


def test_a_rule_that_cannot_be_checked_is_refused(tmp_path: Path) -> None:
    """A timestamp rule on a uuid must not absorb the change: it is refused."""
    v1 = _write_version(tmp_path / "v1", "noisy_fixture_v1", ())
    old_passes = _passes(tmp_path / "old", "noisy_fixture_v1", v1, ("uuid_frozen",))
    new_passes = _passes(tmp_path / "new", "noisy_fixture_v1", v1, ("uuid_frozen",))
    wrong = _rules_file(
        tmp_path,
        "wrong",
        [{"id": "bad", "path": "$.value", "type": "timestamp",
          "reason": "deliberately wrong, must be refused"}],
        case_set_hash(CASES),
    )
    verdict = compare(
        old_passes[0].by_case()["0101-uuid-grammar"],
        new_passes[0].by_case()["0101-uuid-grammar"],
        load_rules(wrong, case_set_hash=case_set_hash(CASES)),
    )
    assert verdict.report_class == "different"
    assert "refused" in verdict.reason


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        print(_table(run_scenarios(Path(directory))))
