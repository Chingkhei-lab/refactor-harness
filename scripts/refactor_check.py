"""Twelve harmless refactors and one hand-seeded bug, each run through the harness.

A behaviour harness is only useful if it can tell a refactor from a defect. The
refactors are the control group: every one must come out `identical` (or
`equivalent`), and the seeded bug — hidden inside an otherwise harmless refactor
— must come out `different`. A false positive here is a harness bug, not a target
finding.

Six refactors are mechanical (formatter, definition order, a `libcst` codemod,
`%` to f-string, loop to comprehension, extract-method); six are hand-written.
Each is a function of the source text, so it is reproducible and reviewable.

Usage:

    python scripts/refactor_check.py --target .harness/target
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from harness import determinism  # noqa: E402
from harness.cases import case_set_hash, load_cases  # noqa: E402
from harness.compare import compare_sets, summarise  # noqa: E402
from harness.runner import Pass, run_pass  # noqa: E402

MODULE = "parse"
NEWLINE = chr(10)
Source = str
Transform = Callable[[Source], Source]


def _replace_once(source: Source, old: str, new: str) -> Source:
    if old not in source:
        raise AssertionError(f"refactor does not apply: {old.splitlines()[0][:60]!r}")
    return source.replace(old, new, 1)


# --- mechanical -------------------------------------------------------------


def ruff_format(source: Source) -> Source:
    """`ruff format`: layout only."""
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--stdin-filename", "__init__.py", "-"],
        input=source,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def reorder_definitions(source: Source) -> Source:
    """Move `percentage` below the classes: definition order is not behaviour."""
    start = source.index("def percentage(string, match):")
    end = source.index("class FixedTzOffset(tzinfo):")
    block = source[start:end]
    without = source[:start] + source[end:]
    marker = without.index("class Parser(object):")
    return without[:marker] + block + without[marker:]


def libcst_rename(source: Source) -> Source:
    """A `libcst` codemod renaming a local: `string` -> `text` in `percentage`.

    libcst round-trips formatting exactly, so the diff is the rename and nothing
    else — unlike an AST unparse, which reformats the whole module.
    """
    import libcst as cst

    class Rename(cst.CSTTransformer):
        def __init__(self) -> None:
            super().__init__()
            self.function = ""

        def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
            self.function = node.name.value

        def leave_FunctionDef(
            self, original: cst.FunctionDef, updated: cst.FunctionDef
        ) -> cst.FunctionDef:
            self.function = ""
            return updated

        def leave_Name(self, original: cst.Name, updated: cst.Name) -> cst.Name:
            if self.function == "percentage" and original.value == "string":
                return updated.with_changes(value="text")
            return updated

    return cst.parse_module(source).visit(Rename()).code


def percent_to_fstring(source: Source) -> Source:
    """`"<%s %s %s>" % (...)` -> f-string, in `FixedTzOffset.__repr__`."""
    return _replace_once(
        source,
        'return "<%s %s %s>" % (self.__class__.__name__, self._name, self._offset)',
        'return f"<{self.__class__.__name__} {self._name} {self._offset}>"',
    )


def loop_to_comprehension(source: Source) -> Source:
    """A for/append loop becomes a comprehension through an extracted helper."""
    old = (
        "        e = []" + NEWLINE
        + "        for part in PARSE_RE.split(self._format):" + NEWLINE
        + "            if not part:" + NEWLINE
        + "                continue" + NEWLINE
        + '            elif part == "{{":' + NEWLINE
        + '                e.append(r"\\{")' + NEWLINE
        + '            elif part == "}}":' + NEWLINE
        + '                e.append(r"\\}")' + NEWLINE
        + '            elif part[0] == "{" and part[-1] == "}":' + NEWLINE
        + "                # this will be a braces-delimited field to handle" + NEWLINE
        + "                e.append(self._handle_field(part))" + NEWLINE
        + "            else:" + NEWLINE
        + "                # just some text to match" + NEWLINE
        + "                e.append(REGEX_SAFETY.sub(self._regex_replace, part))" + NEWLINE
        + '        return "".join(e)'
    )
    new = (
        "        parts = PARSE_RE.split(self._format)" + NEWLINE
        + '        return "".join(self._render_part(part) for part in parts if part)'
    )
    source = _replace_once(source, old, new)
    helper = (
        "    def _render_part(self, part):" + NEWLINE
        + '        if part == "{{":' + NEWLINE
        + '            return r"\\{"' + NEWLINE
        + '        if part == "}}":' + NEWLINE
        + '            return r"\\}"' + NEWLINE
        + '        if part[0] == "{" and part[-1] == "}":' + NEWLINE
        + "            # this will be a braces-delimited field to handle" + NEWLINE
        + "            return self._handle_field(part)" + NEWLINE
        + "        # just some text to match" + NEWLINE
        + "        return REGEX_SAFETY.sub(self._regex_replace, part)" + NEWLINE
        + NEWLINE
        + "    def _generate_expression(self):"
    )
    return _replace_once(source, "    def _generate_expression(self):", helper)


def extract_method(source: Source) -> Source:
    """Pull the base-name split out of `_expand_named_fields` into a helper."""
    old = ('            # split \'aaa[bbb][ccc]...\' into \'aaa\' and \'[bbb][ccc]...\'' + NEWLINE
           + '            n = field.find("[")' + NEWLINE
           + "            if n == -1:" + NEWLINE
           + '                basename, subkeys = field, ""' + NEWLINE
           + "            else:" + NEWLINE
           + "                basename, subkeys = field[:n], field[n:]")
    new = "            basename, subkeys = self._split_field_name(field)"
    source = _replace_once(source, old, new)
    helper = (
        "    @staticmethod" + NEWLINE
        + "    def _split_field_name(field):" + NEWLINE
        + '        """\'aaa[bbb]\' -> (\'aaa\', \'[bbb]\'); a bare name has no subkeys."""'
        + NEWLINE
        + '        n = field.find("[")' + NEWLINE
        + "        if n == -1:" + NEWLINE
        + '            return field, ""' + NEWLINE
        + "        return field[:n], field[n:]"
        + NEWLINE + NEWLINE
        + "    def _expand_named_fields(self, named_fields):"
    )
    return _replace_once(source, "    def _expand_named_fields(self, named_fields):", helper)


# --- hand-written -----------------------------------------------------------


def hand_rename_locals(source: Source) -> Source:
    """Rename `e` -> `parts` inside `_generate_expression` (all uses)."""
    start = source.index("    def _generate_expression(self):")
    end = source.index("    def _to_group_name(self, field):")
    body = src = source[start:end]
    body = body.replace(" e = []", " parts = []")
    body = body.replace("e.append", "parts.append")
    body = body.replace('return "".join(e)', 'return "".join(parts)')
    assert body != src
    return source[:start] + body + source[end:]


def hand_restructure_branch(source: Source) -> Source:
    """Hoist the default out of if/elif/else: same behaviour, one branch less."""
    old = (
        '        if string[0] == "-":' + NEWLINE
        + "            sign = -1" + NEWLINE
        + "            number_start = 1" + NEWLINE
        + '        elif string[0] == "+":' + NEWLINE
        + "            sign = 1" + NEWLINE
        + "            number_start = 1" + NEWLINE
        + "        else:" + NEWLINE
        + "            sign = 1" + NEWLINE
        + "            number_start = 0"
    )
    new = (
        "        sign = 1" + NEWLINE
        + "        number_start = 0" + NEWLINE
        + '        if string[0] == "-":' + NEWLINE
        + "            sign = -1" + NEWLINE
        + "            number_start = 1" + NEWLINE
        + '        elif string[0] == "+":' + NEWLINE
        + "            number_start = 1"
    )
    return _replace_once(source, old, new)


def hand_docstring(source: Source) -> Source:
    """Reword a docstring: it changes `__doc__`, which no case observes."""
    return _replace_once(
        source,
        '"""Fixed offset in minutes east from UTC."""',
        '"""A fixed offset, in minutes, east of UTC."""',
    )


def hand_constant_hoist(source: Source) -> Source:
    """Hoist a literal into a module constant (same value, same use)."""
    return _replace_once(
        source,
        "def percentage(string, match):" + NEWLINE
        + "    return float(string[:-1]) / 100.0",
        "PERCENT = 100.0" + NEWLINE * 3
        + "def percentage(string, match):" + NEWLINE
        + "    return float(string[:-1]) / PERCENT",
    )


def hand_comment_churn(source: Source) -> Source:
    """Comment and blank-line churn in the middle of the file."""
    return _replace_once(
        source,
        "# turn my _format attribute into the _expression attribute",
        "# Build the matching expression from the format (same logic, new comment)",
    )


def hand_alias(source: Source) -> Source:
    """Add an alias for an existing class: a new name, the same object."""
    return _replace_once(
        source,
        "class Result(object):",
        "ParserClass = Parser" + NEWLINE * 3 + "class Result(object):",
    )


# --- the control group and the seeded defect --------------------------------


def seeded_bug(source: Source) -> Source:
    """A harmless-looking refactor with one real change: the sign test flips.

    The renames, the comment churn and the hoisted constant are the disguise;
    the defect is that `"-"` becomes `"+"` in `int_convert.__call__`, so a
    negative number parses as positive.
    """
    source = hand_rename_locals(source)
    source = hand_comment_churn(source)
    source = hand_constant_hoist(source)
    return _replace_once(
        source,
        '        if string[0] == "-":' + NEWLINE
        + "            sign = -1",
        '        if string[0] == "+":' + NEWLINE
        + "            sign = -1",
    )


REFACTORS: dict[str, tuple[str, Transform]] = {
    "ruff-format": ("mechanical: `ruff format`", lambda s: ruff_format(s)),
    "reorder-definitions": ("mechanical: move a definition", reorder_definitions),
    "libcst-rename-local": ("mechanical: libcst codemod renames a local", libcst_rename),
    "percent-to-fstring": ("mechanical: `%` to an f-string", percent_to_fstring),
    "loop-to-comprehension": ("mechanical: loop to a comprehension", loop_to_comprehension),
    "extract-method": ("mechanical: extract a helper", extract_method),
    "hand-rename-locals": ("hand: rename every local in one method", hand_rename_locals),
    "hand-restructure-branch": (
        "hand: hoist a default out of if/elif/else",
        hand_restructure_branch,
    ),
    "hand-docstring": ("hand: reword a docstring", hand_docstring),
    "hand-constant-hoist": ("hand: hoist a literal to a constant", hand_constant_hoist),
    "hand-comment-churn": ("hand: comment and blank-line churn", hand_comment_churn),
    "hand-class-alias": ("hand: add an alias for an existing class", hand_alias),
    "seeded-bug": ("CONTROL: refactor with one real change inside", seeded_bug),
}


def _pass(checkout: Path, cases: list[Any], shim: Path, workdir: Path, index: int) -> Pass:
    return run_pass(
        python=sys.executable,
        checkout=checkout,
        shim_dir=shim,
        src_dir=_REPO_ROOT / "src",
        target_paths=[checkout],
        cases=cases,
        index=index,
        module=MODULE,
        workdir=workdir,
        case_budget_seconds=30.0,
        cpu_budget_seconds=10.0,
        expected_case_set_hash=case_set_hash(cases),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default=".harness/target")
    parser.add_argument("--cases", default="harness/cases/parse.json")
    parser.add_argument("--out", default="docs/results")
    args = parser.parse_args()

    target = (_REPO_ROOT / args.target).resolve()
    original = (target / MODULE / "__init__.py").read_text(encoding="utf-8")
    cases = load_cases(_REPO_ROOT / args.cases)
    shim = determinism.write_shim(_REPO_ROOT / ".harness" / "shim")
    work = _REPO_ROOT / ".harness" / "refactors"
    work.mkdir(parents=True, exist_ok=True)

    baseline = _pass(target, cases, shim, work / "baseline", 1).by_case()
    rows: list[dict[str, Any]] = []
    for index, (name, (kind, transform)) in enumerate(REFACTORS.items(), start=1):
        checkout = work / name
        if checkout.exists():
            shutil.rmtree(checkout)
        shutil.copytree(target, checkout)
        try:
            mutated = transform(original)
        except AssertionError as exc:
            rows.append({"name": name, "kind": kind, "applied": False, "note": str(exc)})
            print(f"[{index}] {name:24} NOT APPLICABLE: {exc}")
            continue
        (checkout / MODULE / "__init__.py").write_text(mutated, encoding="utf-8")
        ran = _pass(checkout, cases, shim, work / f"run-{index}", index + 1)
        summary = summarise(list(compare_sets(baseline, ran.by_case()).values()))
        verdict = "different" if summary["different"] else "clean"
        rows.append(
            {
                "name": name,
                "kind": kind,
                "applied": True,
                "classes": {
                    key: summary[key]
                    for key in (
                        "identical",
                        "equivalent",
                        "equivalent_bounded",
                        "different",
                        "unsupported",
                    )
                },
                "verdict": verdict,
                "line_delta": len(mutated.splitlines()) - len(original.splitlines()),
            }
        )
        print(f"[{index}] {name:24} {verdict:10} {rows[-1]['classes']}")

    refactors = [row for row in rows if row.get("verdict") == "clean"
                 and not row["name"].startswith("seeded")]
    seeded = next(row for row in rows if row["name"] == "seeded-bug")
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "case_set_hash": case_set_hash(cases),
        "case_count": len(cases),
        "rows": rows,
        "refactors_clean": len(refactors),
        "false_positives": [
            row["name"] for row in rows
            if not row["name"].startswith("seeded")
            and row.get("classes", {}).get("different")
        ],
        "seeded_bug_detected": seeded.get("verdict") == "different",
        "seeded_bug_differing_cases": seeded.get("classes", {}).get("different", 0),
        "not_applicable": [row["name"] for row in rows if not row.get("applied")],
    }
    out_dir = _REPO_ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    (out_dir / f"refactors-{stamp}.json").write_text(
        json.dumps(report, indent=2) + NEWLINE, encoding="utf-8"
    )
    print(
        f"clean refactors: {len(refactors)} | false positives: {len(report['false_positives'])}"
        f" | seeded bug detected: {report['seeded_bug_detected']} "
        f"({report['seeded_bug_differing_cases']} cases)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
