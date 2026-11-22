"""Build the hidden case set for the agent gate (Checkpoint 15, design §6).

The visible set is what the agent may run `run_verify` against. The hidden set is
what `finish` is scored on, and it exists so that "passes the harness" cannot mean
"passes the cases I was shown". Two portions, and the second is the one that
matters:

* **grammar** — new option/value combinations the visible set never used, drawn
  from the same pool the visible probes came from, with values the visible set does
  not contain (a `--max-line-length` of 60 where the visible set uses 79, and so on);
* **perturbed** — the visible cases with their **literals changed**: integers,
  string literals and identifiers. A patch that special-cases an input it has seen
  (`if text == "x = 1\\n"`) still passes the visible set and fails here. That is the
  attack this portion exists to catch, and it is why the perturbation changes
  literals rather than adding new inputs.

Nothing here is derived from the expected outputs: the target's annotated codes in
`testing/data` are never read, before or after perturbation.

The set is deduplicated against the visible set by a canonical hash of the case
body, and the **overlap fraction is reported** rather than assumed to be zero: a
perturbation that collapses back onto the case it came from would make the hidden
set a second copy of the visible one, and the report has to say so.

It is generated **before any agent run** and its hash is recorded, so a run's
headline number cannot be produced by regenerating the set afterwards.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "src"))

from harness.cases import Case, case_set_hash  # noqa: E402

OUT = REPO_ROOT / "harness" / "cases" / "pycodestyle-cli-hidden.json"
VISIBLE = REPO_ROOT / "harness" / "cases" / "pycodestyle-cli.json"

#: The stage-3 target gets a hidden set too: its cases are call cases (a source
#: snippet in `args`) rather than CLI files, so the perturbation works on `args`.
MCCABE_OUT = REPO_ROOT / "harness" / "cases" / "mccabe-hidden.json"
MCCABE_VISIBLE = REPO_ROOT / "harness" / "cases" / "mccabe.json"

RULES = {
    "pycodestyle": (REPO_ROOT / "harness" / "rules" / "pycodestyle.json",
                    REPO_ROOT / "harness" / "rules" / "pycodestyle-hidden.json"),
    "mccabe": (REPO_ROOT / "harness" / "rules" / "mccabe.json",
               REPO_ROOT / "harness" / "rules" / "mccabe-hidden.json"),
}


def _write_rules(target: str, hidden_hash: str, check: bool) -> str:
    """Rebind the target's rules to the hidden case-set hash.

    Rules are bound to a case set, and the hidden set is a different case set — the
    loader refused to compare against it until this file existed, which is the
    guardrail working. The rule *content* is unchanged; only the hash it names is.
    """
    source, dest = RULES[target]
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["case_set_hash"] = hidden_hash
    payload["notes"] = (
        "Hidden binding of " + source.name + ": identical rule content, bound to the "
        "hidden case-set hash so the comparator can be run against the hidden record."
    )
    text = json.dumps(payload, indent=1, sort_keys=True) + chr(10)
    if check:
        if not dest.exists() or dest.read_text(encoding="utf-8") != text:
            return "stale"
        return "ok"
    dest.write_text(text, encoding="utf-8")
    return "written"

#: The seed nobody used for the visible set. Recorded in the output file, because a
#: hidden set whose seed is not recorded cannot be regenerated, and a set that
#: cannot be regenerated is not evidence.
SEED = 2027

ENTRY = "pycodestyle.py"

#: The same pools the visible probes draw from, with values the visible set does
#: not use. The visible set has 79/0/3/20/1; these are different numbers on the same
#: options, which is what makes them coverage of the same grammar rather than new
#: grammar the visible set never touched.
GRAMMAR: list[tuple[str, str, tuple[str, ...], str, dict[str, str]]] = [
    ("len-60", "max-line-length 60", ("--max-line-length=60", "sample.py"), "",
     {"sample.py": "s = '" + "a" * 61 + "'\n"}),
    ("len-100", "max-line-length 100", ("--max-line-length=100", "sample.py"), "",
     {"sample.py": "s = '" + "b" * 99 + "'\n"}),
    ("len-equal", "a line exactly at the limit", ("--max-line-length=10", "sample.py"), "",
     {"sample.py": "abcdefghi\nabcdefghij\n"}),
    ("blank-1", "max-blank-lines 1", ("--max-blank-lines=1", "sample.py"), "",
     {"sample.py": "x = 1\n\n\ny = 2\n\n\n\nz = 3\n"}),
    ("indent-2", "indent-size 2", ("--indent-size=2", "sample.py"), "",
     {"sample.py": "if True:\n  x = 1\n"}),
    ("indent-8", "indent-size 8", ("--indent-size=8", "sample.py"), "",
     {"sample.py": "if True:\n        x = 1\n"}),
    ("doc-30", "max-doc-length 30", ("--max-doc-length=30", "sample.py"), "",
     {"sample.py": '"""' + "c" * 40 + '"""\n'}),
    ("select-e2", "select E2 only", ("--select=E2", "sample.py"), "",
     {"sample.py": "x=1\ny =  2\n"}),
    ("select-w", "select W only", ("--select=W", "sample.py"), "",
     {"sample.py": "if True:\n\tpass\n"}),
    ("ignore-e5", "ignore E501 only", ("--ignore=E501", "sample.py"), "",
     {"sample.py": "s = '" + "d" * 90 + "'\nx=1\n"}),
    ("config-79", "config file sets 60", ("--config=cfg.ini", "sample.py"), "",
     {"sample.py": "s = '" + "e" * 70 + "'\n",
      "cfg.ini": "[pycodestyle]\nmax-line-length = 60\n"}),
    ("config-select", "config file selects E7", ("sample.py",), "",
     {"sample.py": "if type(1) == int:\n    pass\n",
      "setup.cfg": "[pycodestyle]\nselect = E7\n"}),
    ("diff-one-line", "single-line diff, changed content",
     ("--diff", "sample.py"), "--- sample.py\n+++ sample.py\n@@ -1 +1 @@\n-q=1\n+q=9\n",
     {"sample.py": "q=1\n"}),
    ("diff-added-context", "diff with more context",
     ("--diff", "sample.py"),
     "--- sample.py\n+++ sample.py\n@@ -1,3 +1,3 @@\n-a=1\n+a=2\n b=2\n c=3\n",
     {"sample.py": "a=1\nb=2\nc=3\n"}),
    ("stdin-clean", "stdin with nothing to report", ("-",), "s = 1\n", {}),
    ("statistics-only", "statistics without count",
     ("--statistics", "sample.py"), "", {"sample.py": "x=1\ny=2\nz=3\n"}),
    ("count-only", "count without statistics", ("--count", "sample.py"), "",
     {"sample.py": "x=1\n"}),
    ("quiet-double", "two quiet flags with findings", ("-qq", "sample.py"), "",
     {"sample.py": "x=1\n"}),
    ("exclude-dir", "exclude a directory", ("--exclude=skip", "."), "",
     {"sample.py": "x=1\n", "skip/bad.py": "y=2\n"}),
    ("filename-only", "filename pattern selects one", ("--filename=keep*.py", "."), "",
     {"keep.py": "x=1\n", "other.py": "y=2\n"}),
    ("two-files", "two files in one call", ("one.py", "two.py"), "",
     {"one.py": "x=1\n", "two.py": "y=2\n"}),
    ("empty-file", "an empty input file", ("empty.py",), "", {"empty.py": ""}),
    ("crlf", "CRLF line endings", ("sample.py",), "",
     {"sample.py": "x=1\r\ny =2\r\n"}),
    ("no-newline-at-eof", "a file without a trailing newline", ("sample.py",), "",
     {"sample.py": "x=1"}),
]

#: Identifiers exist in the target's own data files; renaming them is the cheapest
#: way to make a literal-copied patch fail while keeping the input plausible.
IDENTIFIERS = ("x", "y", "z", "foo", "bar", "baz", "sample", "change", "parser")

_NUMBER = re.compile(r"(?<![\w.])(\d+)(?![\w.])")
_STRING = re.compile(r"('(?:[^'\\\n]|\\.)*'|\"(?:[^\"\\\n]|\\.)*\")")
_IDENT = re.compile(r"\b(" + "|".join(IDENTIFIERS) + r")\b")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _body(case: Case) -> str:
    """The part of a case that decides behaviour, for dedupe and overlap."""
    payload = case.body()
    payload.pop("id", None)
    payload.pop("source", None)
    return json.dumps(payload, sort_keys=True)


def perturb(text: str, seed: int) -> str:
    """Change every literal in an input, deterministically.

    Numbers shift, strings get a suffix, identifiers get a suffix. The result is
    still the same *shape* of input, so the case still exercises the same code path
    — which is the point: a special-cased patch survives a new shape, not a new
    literal in the shape it already handles.
    """
    tag = str(seed % 97)

    def number(match: re.Match[str]) -> str:
        value = match.group(1)
        return value + tag if len(value) < 4 else str(int(value) + 1)

    def string(match: re.Match[str]) -> str:
        raw = match.group(1)
        body = raw[1:-1]
        return raw[0] + body + tag + raw[0]

    def ident(match: re.Match[str]) -> str:
        return match.group(1) + tag

    text = _STRING.sub(string, text)
    text = _NUMBER.sub(number, text)
    return _IDENT.sub(ident, text)


def _perturbed_files(files: dict[str, str], seed: int) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, content in sorted(files.items()):
        # The *file name* is perturbed too: a patch that keys on `E25.py` must not
        # survive because the hidden set only changed the file's contents.
        stem, _, suffix = name.partition(".")
        new_name = f"{stem}_h{suffix and '.' + suffix}"
        out[new_name] = perturb(content, seed)
    return out


def build() -> tuple[dict[str, object], str, dict[str, object]]:
    if not VISIBLE.exists():
        raise SystemExit(f"{VISIBLE} missing: build the visible set first")
    from harness.cases import load_cases

    visible = json.loads(VISIBLE.read_text(encoding="utf-8"))
    visible_cases = load_cases(VISIBLE)
    visible_bodies = {_body(case) for case in visible_cases}

    cases: list[Case] = []
    counts = {"grammar": 0, "perturbed": 0}
    duplicates = {"grammar": 0, "perturbed": 0}

    for name, dimension, argv, stdin, files in GRAMMAR:
        case = Case(
            id=f"hidden-{name}",
            source=f"hidden/grammar/{dimension}",
            entry=ENTRY,
            args=(),
            kind="cli",
            argv=argv,
            stdin=stdin,
            files=files,
        )
        counts["grammar"] += 1
        if _body(case) in visible_bodies:
            duplicates["grammar"] += 1
            continue
        cases.append(case)

    for index, visible_case in enumerate(visible_cases):
        if visible_case.kind != "cli":
            continue
        files = _perturbed_files(dict(visible_case.files), SEED + index)
        argv = tuple(perturb(arg, SEED + index) for arg in visible_case.argv)
        if not files and not visible_case.stdin:
            # An input with no content cannot be perturbed: the empty-argument
            # probes would come out byte-identical and be deduplicated anyway. Skip
            # them explicitly so the overlap number measures the perturbation and
            # not this case.
            continue
        case = Case(
            id=f"hidden-perturbed-{visible_case.id}",
            source="hidden/perturbed/literals-changed",
            entry=visible_case.entry,
            args=(),
            kind="cli",
            argv=argv,
            stdin=perturb(visible_case.stdin, SEED + index),
            files=files,
        )
        counts["perturbed"] += 1
        if _body(case) in visible_bodies:
            duplicates["perturbed"] += 1
            continue
        cases.append(case)

    total = counts["grammar"] + counts["perturbed"]
    overlap = {
        "candidates": total,
        "kept": len(cases),
        "duplicates": duplicates,
        "overlap_fraction": round((total - len(cases)) / total, 4) if total else 0.0,
        "against": "the visible case set, by canonical case body",
    }
    payload = {
        "schema": "harness-cases/1",
        "target": "pycodestyle 2.15.0",
        "kind": "cli",
        "hidden": True,
        "seed": SEED,
        "visible_case_set_hash": visible["case_set_hash"],
        "source_tree_sha256": visible["source_tree_sha256"],
        "inputs_only": "expected codes in the data files are never read",
        "counts_by_source": counts,
        "overlap": overlap,
        "case_set_hash": case_set_hash(cases),
        "cases": [case.body() for case in cases],
    }
    return payload, case_set_hash(cases), overlap


def build_mccabe() -> tuple[dict[str, object], str, dict[str, object]]:
    """The mccabe hidden set: perturbed copies of the visible cases, plus grammar.

    A snippet case is a source string, so perturbing its identifiers and literals is
    the whole job; a CLI case is perturbed as in the pycodestyle set. The `api` case
    is not carried over: its input *is* the module's surface, and there is no
    literal in it to change — saying so is better than counting it as hidden.
    """
    from harness.cases import load_cases

    visible = json.loads(MCCABE_VISIBLE.read_text(encoding="utf-8"))
    visible_cases = load_cases(MCCABE_VISIBLE)
    visible_bodies = {_body(case) for case in visible_cases}

    cases: list[Case] = []
    counts = {"grammar": 0, "perturbed": 0}
    duplicates = {"grammar": 0, "perturbed": 0}

    for index, case in enumerate(visible_cases):
        if case.kind == "api":
            continue
        if case.kind == "cli":
            hidden = Case(
                id=f"hidden-perturbed-{case.id}",
                source="hidden/perturbed/literals-changed",
                entry=case.entry,
                args=(),
                kind="cli",
                argv=tuple(perturb(arg, SEED + index) for arg in case.argv),
                stdin=perturb(case.stdin, SEED + index),
                files=_perturbed_files(dict(case.files), SEED + index),
            )
        else:
            hidden = Case(
                id=f"hidden-perturbed-{case.id}",
                source="hidden/perturbed/literals-changed",
                entry=case.entry,
                args=tuple(
                    perturb(arg, SEED + index) if isinstance(arg, str) else arg
                    for arg in case.args
                ),
                kwargs={
                    key: perturb(value, SEED + index) if isinstance(value, str) else value
                    for key, value in case.kwargs.items()
                },
                kind="call",
            )
        counts["perturbed"] += 1
        if _body(hidden) in visible_bodies:
            duplicates["perturbed"] += 1
            continue
        cases.append(hidden)

    # A handful of grammar cases the visible set never had: neighbours of the
    # complexity boundaries, which is where a rewritten visitor breaks.
    for name, snippet in (
        (
            "nested-if",
            "def f(x):\n    if x:\n        if x:\n            if x:\n                return 1\n",
        ),
        ("loops", "def f(xs):\n    for a in xs:\n        while a:\n            a -= 1\n"),
        (
            "try-except",
            "def f():\n    try:\n        pass\n    except ValueError:\n        pass\n",
        ),
        ("with-open", "def f(p):\n    with open(p) as h:\n        return h\n"),
        ("bool-ops", "def f(a, b, c):\n    return a and b or c\n"),
        ("generator", "def f(xs):\n    return (x for x in xs if x)\n"),
    ):
        counts["grammar"] += 1
        case = Case(
            id=f"hidden-{name}",
            source="hidden/grammar/complexity-boundaries",
            entry="get_code_complexity",
            args=(snippet,),
            kind="call",
        )
        if _body(case) in visible_bodies:
            duplicates["grammar"] += 1
            continue
        cases.append(case)

    total = counts["grammar"] + counts["perturbed"]
    overlap = {
        "candidates": total,
        "kept": len(cases),
        "duplicates": duplicates,
        "overlap_fraction": round((total - len(cases)) / total, 4) if total else 0.0,
        "against": "the visible case set, by canonical case body",
    }
    payload = {
        "schema": "harness-cases/1",
        "target": "mccabe 0.7.0",
        "kind": "call",
        "hidden": True,
        "seed": SEED,
        "visible_case_set_hash": visible["case_set_hash"],
        "inputs_only": "the asserted complexities in test_mccabe.py are never read",
        "counts_by_source": counts,
        "overlap": overlap,
        "case_set_hash": case_set_hash(cases),
        "cases": [case.body() for case in cases],
    }
    return payload, case_set_hash(cases), overlap


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--target", choices=("pycodestyle", "mccabe"), default="pycodestyle")
    args = parser.parse_args(argv)
    if args.target == "mccabe":
        payload, digest, overlap = build_mccabe()
        out = MCCABE_OUT
    else:
        payload, digest, overlap = build()
        out = OUT
    status = _write_rules(args.target, digest, args.check)
    if status == "stale":
        print(f"stale: {args.target}-hidden rules file differs from this binding")
        return 1
    return _write(payload, digest, overlap, out, check=args.check)


def _write(payload, digest, overlap, out, *, check):
    text = json.dumps(payload, indent=1, sort_keys=True) + chr(10)
    if check:
        if not out.exists():
            print("stale: hidden case file does not exist")
            return 1
        if out.read_text(encoding="utf-8") != text:
            print(f"stale: {out.name} differs from what this script generates")
            return 1
        print(
            f"hidden case set ok: {len(payload['cases'])} cases, hash {digest[:12]}, "
            f"overlap {overlap['overlap_fraction']}"
        )
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(
        f"wrote {out.relative_to(REPO_ROOT)}: {len(payload['cases'])} cases "
        f"({overlap['candidates']} candidates, {overlap['duplicates']} duplicates), "
        f"hash {digest[:12]}, overlap {overlap['overlap_fraction']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
