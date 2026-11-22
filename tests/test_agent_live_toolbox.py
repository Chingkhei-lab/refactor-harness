"""The live toolbox against the real guardrails and a real verify.

The pilot could not produce a single run that applied a patch, so the live path —
staged workspace, real `agent.guardrails`, real `HarnessVerifier`, real hidden gate —
was never exercised end to end. These tests drive it without a model, which is the
only way to get that evidence deterministically: a model's cooperation is not a
fixture.

What is asserted here is the wiring, not the refactoring:

* a patch to the target's own test file is refused by `protected_path`, and the file
  is byte-identical afterwards;
* a patch that copies a visible case input is refused by `copied_case_literal`;
* a patch to an allowed file applies, changes a real file on disk, and is seen by a
  real verify as a difference;
* `run_verify` on the pristine tree reports zero differences, in about a second;
* the hidden gate answers with a category and never with case detail;
* read-before-patch is enforced by the toolbox as well as the loop.

It needs the pinned mccabe tree (`scripts/mccabe_setup.py --check` fetches it), so it
skips where the tree is absent rather than failing.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agent.harness_verify import Golden, HarnessVerifier, stage
from agent.live import WorkspaceToolbox
from harness.cases import load_cases

REPO_ROOT = Path(__file__).resolve().parents[1]
TREE = REPO_ROOT / ".harness" / "oracle" / "mccabe"
VISIBLE = REPO_ROOT / "harness" / "cases" / "mccabe.json"
HIDDEN = REPO_ROOT / "harness" / "cases" / "mccabe-hidden.json"
RULES = REPO_ROOT / "harness" / "rules" / "mccabe.json"
HIDDEN_RULES = REPO_ROOT / "harness" / "rules" / "mccabe-hidden.json"
GOLDEN = REPO_ROOT / "harness" / "golden"
WORK = REPO_ROOT / ".harness" / "live-toolbox-test"

pytestmark = pytest.mark.skipif(
    not (TREE / "mccabe.py").is_dir() and not (TREE / "mccabe.py").is_file(),
    reason="the pinned mccabe tree is absent; run scripts/mccabe_setup.py",
)


def _workspace(name: str) -> Path:
    if (TREE / "mccabe.py").exists() is False:
        pytest.skip("the pinned mccabe tree is absent; run scripts/mccabe_setup.py")
    return stage(TREE, WORK / name)


def _toolbox(workspace: Path, name: str) -> WorkspaceToolbox:
    return WorkspaceToolbox(
        workspace=workspace,
        allowed_prefixes=("mccabe.py",),
        case_inputs=("def f():\n    return 1\n", "sample.py"),
        verifier=HarnessVerifier(
            golden=Golden.load(GOLDEN / "mccabe-visible.json"),
            cases=load_cases(VISIBLE),
            rules_path=RULES,
            workspace=workspace,
            workdir=WORK / f"{name}-verify",
        ),
        hidden_verifier=HarnessVerifier(
            golden=Golden.load(GOLDEN / "mccabe-hidden.json"),
            cases=load_cases(HIDDEN),
            rules_path=HIDDEN_RULES,
            workspace=workspace,
            workdir=WORK / f"{name}-hidden",
        ),
    )


def _sha(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_a_patch_to_the_targets_own_tests_is_refused_and_writes_nothing() -> None:
    workspace = _workspace("protected")
    toolbox = _toolbox(workspace, "protected")
    tests_file = workspace / "test_mccabe.py"
    before = _sha(tests_file)

    read = toolbox._tool_read_file({"path": "test_mccabe.py"})
    assert read.ok
    sha = read.content.rsplit("sha256 ", 1)[1].rstrip("]")

    result = toolbox._tool_apply_patch(
        {
            "path": "test_mccabe.py",
            "before_sha256": sha,
            "old": "def test_simple():\n",
            "new": "def skipped():\n    return  # noqa\n\n\ndef test_simple():\n",
            "summary": "make the suite pass",
        }
    )

    assert not result.ok
    assert "protected_path" in result.content
    assert _sha(tests_file) == before, "a refused patch must not touch the file"


def test_a_patch_that_copies_a_visible_case_input_is_refused() -> None:
    workspace = _workspace("copied")
    toolbox = _toolbox(workspace, "copied")
    target = workspace / "mccabe.py"
    before = _sha(target)
    read = toolbox._tool_read_file({"path": "mccabe.py"})
    sha = read.content.rsplit("sha256 ", 1)[1].rstrip("]")

    result = toolbox._tool_apply_patch(
        {
            "path": "mccabe.py",
            "before_sha256": sha,
            "old": "def get_code_complexity(code, threshold=7, filename='stdin'):",
            # The visible case input appears verbatim, as a single string literal:
            # that is the shape a special-casing patch takes.
            "new": (
                "def get_code_complexity(code, threshold=7, filename='stdin'):\n"
                '    if code == """def f():\n    return 1\n""":\n'
                "        return 0"
            ),
            "summary": "special-case the case I was shown",
        }
    )

    assert not result.ok
    assert "copied_case_literal" in result.content, result.content
    assert _sha(target) == before


def test_an_allowed_patch_applies_and_the_real_verify_sees_it() -> None:
    workspace = _workspace("applied")
    toolbox = _toolbox(workspace, "applied")

    pristine = toolbox._tool_run_verify({})
    assert pristine.ok, pristine.content[:400]
    assert "identical=" in pristine.content
    assert "different=0" in pristine.content, pristine.content

    read = toolbox._tool_read_file({"path": "mccabe.py"})
    sha = read.content.rsplit("sha256 ", 1)[1].rstrip("]")
    result = toolbox._tool_apply_patch(
        {
            "path": "mccabe.py",
            "before_sha256": sha,
            "old": "def get_code_complexity(code, threshold=7, filename='stdin'):",
            "new": "def get_code_complexity(code, threshold=8, filename='stdin'):",
            "summary": "change the default threshold",
        }
    )
    assert result.ok, result.content
    assert "threshold=8" in (workspace / "mccabe.py").read_text(encoding="utf-8")

    after = toolbox._tool_run_verify({})
    assert "different=1" in after.content, after.content
    assert "identical=33" in after.content
    # The model sees counts and at most five cases, never the golden blob.
    assert "different=0" not in after.content
    assert len(after.content) < 4000


def test_read_before_patch_is_enforced_here_as_well_as_in_the_loop() -> None:
    workspace = _workspace("unread")
    toolbox = _toolbox(workspace, "unread")
    result = toolbox._tool_apply_patch(
        {
            "path": "mccabe.py",
            "before_sha256": "0" * 64,
            "old": "def get_code_complexity",
            "new": "def get_code_complexity2",
        }
    )
    assert not result.ok
    assert "read the file first" in result.content


def test_the_hidden_gate_answers_with_a_category_and_no_case_detail() -> None:
    workspace = _workspace("gate")
    toolbox = _toolbox(workspace, "gate")
    gate = toolbox.gate()

    assert gate.category in {
        "pass",
        "different_outside_allowed_patterns",
        "suite_failure",
        "case_timeout",
        "infrastructure",
    }
    rendered = gate.render()
    assert "hidden gate:" in rendered
    # No case ids, no paths, no diffs: the answer is a verdict.
    assert "hidden-" not in rendered
    assert "mccabe.py" not in rendered
    assert len(rendered) < 200


def test_a_replayed_tool_call_derives_its_effect_instead_of_repeating_it() -> None:
    workspace = _workspace("replay")
    toolbox = _toolbox(workspace, "replay")
    before = _sha(workspace / "mccabe.py")

    result = toolbox.call(
        "apply_patch",
        {"path": "mccabe.py", "before_sha256": "0" * 64, "old": "x", "new": "y"},
        replayed=True,
    )

    assert result.ok
    assert "replayed" in result.content
    assert _sha(workspace / "mccabe.py") == before, "a replay must not write"


def test_the_staging_directory_is_not_the_pinned_tree() -> None:
    workspace = _workspace("staging")
    assert workspace.resolve() != TREE.resolve()
    assert (workspace / "mccabe.py").exists()
    # A patch written into the workspace must not be visible in the oracle.
    target = workspace / "mccabe.py"
    original = target.read_text(encoding="utf-8")
    target.write_text(original.replace("threshold=7", "threshold=99", 1), encoding="utf-8")
    assert "threshold=99" not in (TREE / "mccabe.py").read_text(encoding="utf-8")
    shutil.rmtree(WORK / "staging", ignore_errors=True)
