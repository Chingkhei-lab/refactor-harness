"""Build the stage-2 CLI case set for pycodestyle.

Two sources of cases, both committed and hashed:

  * **inputs** — the first 40 lines of every fixture in the target's own test
    data (`testing/data/*.py`). Inputs only: the annotated expected codes in
    those files are never read, so the case set cannot inherit the suite's
    answers.
  * **option probes** — one case per option-matrix dimension: abbreviation,
    `--`, a negative number, a repeated option, a clustered short flag, an invalid
    choice, a non-integer value, a default, `--help`, `--version`, stdin, and the
    declared-unstable/`unordered` flags.

The selection is fixed: no randomness, so the file and its hash are reproducible
from the pinned tree.

    python scripts/build_cases_stage2.py            # writes harness/cases/pycodestyle-cli.json
    python scripts/build_cases_stage2.py --check     # fails if the file is stale
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from harness.cases import Case, case_set_hash  # noqa: E402

TREE = REPO / ".harness" / "oracle" / "pycodestyle"
OUT = REPO / "harness" / "cases" / "pycodestyle-cli.json"
ENTRY = "pycodestyle.py"
EXCERPT_LINES = 40

# Each probe names the dimension it covers, so the report can say which parts of
# the CLI surface are exercised rather than just how many cases there are.
PROBES: list[tuple[str, str, tuple[str, ...], str, dict[str, str]]] = [
    ("help", "long-help", ("--help",), "", {}),
    ("version", "version", ("--version",), "", {}),
    ("abbreviation", "abbreviated-long-option", ("--max-line-len=79", "sample.py"), "",
     {"sample.py": "x = 1\n"}),
    ("separator", "double-dash-then-filename", ("--", "--not-an-option.py"), "",
     {"--not-an-option.py": "x=1\n"}),
    ("negative", "negative-number-as-value", ("--max-line-length=-1", "sample.py"), "",
     {"sample.py": "x = 1\n"}),
    ("repeat", "repeated-option-last-wins", ("--select=E1", "--select=E2", "sample.py"), "",
     {"sample.py": "x=1\ny =2\n"}),
    ("cluster", "clustered-short-flags", ("-qq", "sample.py"), "", {"sample.py": "x=1\n"}),
    ("invalid-choice", "invalid-value-for-choice", ("--format=not-a-format", "sample.py"), "",
     {"sample.py": "x = 1\n"}),
    ("int-error", "non-integer-where-int-expected", ("--max-line-length=abc", "sample.py"),
     "", {"sample.py": "x = 1\n"}),
    ("defaults", "no-options-default-behaviour", ("sample.py",), "", {"sample.py": "x=1\n"}),
    ("stdin", "dash-reads-stdin", ("-",), "x=1\n", {}),
    ("unknown", "unknown-option", ("--no-such-option", "sample.py"), "",
     {"sample.py": "x = 1\n"}),
    ("missing-file", "file-does-not-exist", ("absent.py",), "", {}),
    ("blank-lines", "max-blank-lines", ("--max-blank-lines=0", "sample.py"), "",
     {"sample.py": "x = 1\n\n\ny = 2\n"}),
    ("indent", "indent-size", ("--indent-size=3", "sample.py"), "",
     {"sample.py": "if True:\n   x = 1\n"}),
    ("doc-length", "max-doc-length", ("--max-doc-length=20", "sample.py"), "",
     {"sample.py": '"""' + "a" * 30 + '"""\n'}),
    ("statistics", "statistics-and-count", ("--statistics", "--count", "sample.py"), "",
     {"sample.py": "x=1\ny=2\n"}),
    ("benchmark", "declared-unstable-benchmark", ("--benchmark", "sample.py"), "",
     {"sample.py": "x = 1\n"}),
    ("jobs", "declared-unordered-jobs", ("--jobs=1", "sample.py"), "",
     {"sample.py": "x = 1\n"}),
    # --- the argument-handling surface a parser swap rewrites ----------------
    # Added after the first coverage run showed `read_config` at 14/46 lines and
    # `process_options` at 18/27: config files, `--diff`, reporters and the
    # multi-option parsers live there, and they are exactly where optparse and
    # argparse behave differently.
    ("config-file", "explicit-config-file",
     ("--config=cfg.ini", "sample.py"), "",
     {"sample.py": "x=1\n", "cfg.ini": "[pycodestyle]\nmax-line-length = 10\n"}),
    ("local-config", "project-config-in-case-dir", ("sample.py",), "",
     {"sample.py": "x=1\n", "setup.cfg": "[pycodestyle]\nmax-line-length = 10\n"}),
    ("deprecated-section", "deprecated-pep8-section", ("sample.py",), "",
     {"sample.py": "x=1\n", "setup.cfg": "[pep8]\nmax-line-length = 10\n"}),
    ("config-unknown-key", "unknown-key-in-config", ("sample.py",), "",
     {"sample.py": "x=1\n", "setup.cfg": "[pycodestyle]\nnot-a-real-option = 1\n"}),
    ("config-int-error", "bad-integer-in-config", ("sample.py",), "",
     {"sample.py": "x=1\n", "setup.cfg": "[pycodestyle]\nmax-line-length = abc\n"}),
    ("verbose", "single-verbose", ("-v", "sample.py"), "", {"sample.py": "x = 1\n"}),
    ("very-verbose", "double-verbose", ("-vv", "sample.py"), "",
     {"sample.py": "x = 1\n", "setup.cfg": "[pycodestyle]\nmax-line-length = 10\n"}),
    ("no-args-with-config", "no-args-and-project-config", (), "",
     {"setup.cfg": "[pycodestyle]\nmax-line-length = 10\n"}),
    ("diff-stdin", "unified-diff-on-stdin", ("--diff", "sample.py"),
     "--- sample.py\n+++ sample.py\n@@ -1 +1 @@\n-x=1\n+x=2\n", {"sample.py": "x=1\n"}),
    ("format-pylint", "pylint-report-format", ("--format=pylint", "sample.py"), "",
     {"sample.py": "x=1\n"}),
    ("format-custom", "custom-report-format",
     ("--format=%(path)s:%(row)d:%(code)s", "sample.py"), "", {"sample.py": "x=1\n"}),
    ("show-source-and-pep8", "source-and-pep8-text",
     ("--show-source", "--show-pep8", "sample.py"), "", {"sample.py": "x=1\n"}),
    ("exclude-and-filename", "exclude-and-filename-patterns",
     ("--exclude=sample.py", "--filename=*.py", "."), "", {"sample.py": "x=1\n"}),
    ("hang-closing", "hang-closing-indent", ("--hang-closing", "sample.py"), "",
     {"sample.py": "print('x')\n        )\n"}),
    ("select-ignore", "select-and-ignore-together",
     ("--select=E,W", "--ignore=E501", "sample.py"), "", {"sample.py": "x=1\n"}),
    # No arguments and no project config: `parser.error('input not specified')`.
    # The upstream suite asserts this text exactly, so the case exists to make the
    # upstream-vs-harness comparison table complete rather than inferred.
    ("no-args", "no-arguments-no-config", (), "", {}),
]


def _excerpt(path: Path) -> str:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    return "".join(lines[:EXCERPT_LINES])


def build() -> tuple[dict[str, object], str]:
    data = TREE / "testing" / "data"
    if not data.is_dir():
        raise SystemExit(
            f"{data} missing: run scripts/fetch_target.py --target pycodestyle first"
        )
    cases: list[Case] = []
    counts: dict[str, int] = {"fixtures": 0, "probes": 0, "api": 0}
    # Sort by name (a str), never by Path: PurePath comparison is case-normalised
    # on Windows and case-sensitive on POSIX, so `sorted(Path)` puts `crlf.py`
    # before `E10.py` on Windows and after it on Linux. The case-set hash then
    # depends on the machine that built the file — measured, on CI.
    for path in sorted(data.glob("*.py"), key=lambda item: item.name):
        name = path.name
        cases.append(
            Case(
                id=f"fixture-{name.replace('.py', '').lower()}",
                source="testing/data",
                entry=ENTRY,
                args=(),
                kind="cli",
                argv=(name,),
                files={name: _excerpt(path)},
            )
        )
        counts["fixtures"] += 1
    for name, dimension, argv, stdin, files in PROBES:
        cases.append(
            Case(
                id=f"probe-{name}",
                source=f"options/{dimension}",
                entry=ENTRY,
                args=(),
                kind="cli",
                argv=argv,
                stdin=stdin,
                files=files,
            )
        )
        counts["probes"] += 1
    # API snapshots: the surface, not a call. `api-parser` is the case that would
    # have caught the one difference the upstream suite saw and the CLI cases
    # could not — `get_parser()` still returns *a* parser, but no longer one with
    # `add_option`.
    for case_id, source, call in (
        ("api-module", "api/surface", {}),
        (
            "api-parser",
            "api/surface-of-a-returned-object",
            {"entry": "get_parser", "args": [], "kwargs": {}},
        ),
    ):
        cases.append(
            Case(
                id=case_id,
                source=source,
                entry="pycodestyle",
                args=(),
                kind="api",
                module="pycodestyle",
                call=call,
            )
        )
        counts["api"] += 1
    payload = {
        "schema": "harness-cases/1",
        "target": "pycodestyle 2.15.0",
        "kind": "cli",
        "source_tree": "https://codeload.github.com/PyCQA/pycodestyle/tar.gz/refs/tags/2.15.0",
        "source_tree_sha256": (
            "3cb6afba1667f13b4ca7884154299108ef8e633a36526e29412a5cec7b5db4bd"
        ),
        "excerpt": f"first {EXCERPT_LINES} lines",
        "inputs_only": "expected codes in the data files are never read",
        "counts_by_source": counts,
        "case_set_hash": case_set_hash(cases),
        "cases": [case.body() for case in cases],
    }
    return payload, case_set_hash(cases)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    payload, digest = build()
    text = json.dumps(payload, indent=1, sort_keys=True) + "\n"
    if args.check:
        if not OUT.exists():
            print("stale: case file does not exist")
            return 1
        current = json.loads(OUT.read_text(encoding="utf-8"))
        fresh = json.loads(text)
        if current != fresh:
            print("stale: case file differs from what the pinned tree produces")
            # Name the first difference: "stale" without a diff is a riddle.
            for key in ("case_set_hash", "counts_by_source"):
                if current.get(key) != fresh.get(key):
                    print(f"  {key}: committed {current.get(key)!r} != fresh {fresh.get(key)!r}")
            old_cases = {c["id"]: c for c in current.get("cases", [])}
            new_cases = {c["id"]: c for c in fresh.get("cases", [])}
            for case_id in sorted(set(old_cases) | set(new_cases)):
                if old_cases.get(case_id) != new_cases.get(case_id):
                    print(f"  first differing case: {case_id}")
                    for field in ("argv", "stdin", "files"):
                        before = old_cases.get(case_id, {}).get(field)
                        after = new_cases.get(case_id, {}).get(field)
                        if before != after:
                            print(f"    {field} committed {before!r}")
                            print(f"    {field} fresh     {after!r}")
                            break
                    break
            return 1
        print(f"fresh: {len(payload['cases'])} cases, hash {digest[:12]}")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(
        f"{len(payload['cases'])} cases "
        f"({payload['counts_by_source']['fixtures']} fixtures + "
        f"{payload['counts_by_source']['probes']} probes), hash {digest[:12]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
