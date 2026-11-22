"""Set up mccabe as the stage-3 target: pinned sdist, oracle tree, cases, canary.

Stage 3 exists because the migration tasks need a target with a *different* kind of
surface from `pycodestyle`: `mccabe` is ~300 lines with a real test suite (16 tests)
and a hand-written argument parser in `main`, which is what `mig-manual-argparse`
asks an agent to replace.

Everything here is reproducible from the pinned artifact:

    python scripts/mccabe_setup.py --check     # verify the pins and the case set
    python scripts/mccabe_setup.py             # fetch, unpack, build cases

The case set is seeded from the target's **own test inputs** — the source snippets
in `test_mccabe.py` that the suite feeds to `get_code_complexity` — because those
are inputs, not answers. The suite's asserted complexity values are never read: a
case that inherited them would inherit the oracle's opinion, which is what the
harness is supposed to form independently.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
import tarfile
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from harness.cases import Case, case_set_hash  # noqa: E402

PIN = {
    "name": "mccabe",
    "version": "0.7.0",
    "url": "https://files.pythonhosted.org/packages/source/m/mccabe/mccabe-0.7.0.tar.gz",
    "sha256": "348e0240c33b60bbdf4e523192ef919f28cb2c3d7d5c7794f74009290f236325",
}
ARCHIVE = REPO / ".harness" / "sdist" / "mccabe-0.7.0.tar.gz"
ORACLE = REPO / ".harness" / "oracle" / "mccabe"
CASES = REPO / "harness" / "cases" / "mccabe.json"
RULES = REPO / "harness" / "rules" / "mccabe.json"
APPROVALS = REPO / "harness" / "approvals" / "mccabe.json"
CANARY = REPO / "docs" / "results" / "mccabe-canary.json"

#: The six entry points mccabe exposes. The `api` cases below snapshot all of them
#: plus the module surface, because a refactor that keeps the CLI intact and breaks
#: `get_code_complexity` is exactly the change a program cannot show.
ENTRIES = (
    "get_code_complexity",
    "PathGraphingAstVisitor",
    "PathGraphingAstVisitor.get_code_complexity",
    "main",
)

#: Seeded from the source snippets in `test_mccabe.py`. Inputs only.
SNIPPETS: tuple[tuple[str, str], ...] = (
    ("plain", "def f():\n    return 1\n"),
    ("if", "def f(x):\n    if x:\n        return 1\n    return 0\n"),
    ("if-else", "def f(x):\n    if x:\n        return 1\n    else:\n        return 0\n"),
    ("elif-chain", "def f(x):\n    if x == 1:\n        pass\n    elif x == 2:\n        pass\n"
                   "    elif x == 3:\n        pass\n"),
    ("for", "def f(items):\n    for item in items:\n        yield item\n"),
    ("while", "def f():\n    while True:\n        break\n"),
    ("and", "def f(a, b):\n    if a and b:\n        return 1\n"),
    ("or", "def f(a, b):\n    if a or b:\n        return 1\n"),
    ("and-or", "def f(a, b, c):\n    if a and b or c:\n        return 1\n"),
    ("try-except", "def f():\n    try:\n        pass\n    except ValueError:\n        pass\n"),
    ("try-finally", "def f():\n    try:\n        pass\n    finally:\n        pass\n"),
    ("with", "def f(handle):\n    with handle:\n        pass\n"),
    ("lambda", "f = lambda x: x\n"),
    ("assert", "def f(x):\n    assert x\n"),
    ("nested-def", "def f():\n    def g():\n        return 1\n    return g\n"),
    ("class-method", "class C:\n    def m(self):\n        return 1\n"),
    ("decorator", "import functools\n\n@functools.wraps\n"
                  "def f():\n    return 1\n"),
    ("comprehension", "def f(items):\n    return [item for item in items if item]\n"),
    ("async-def", "async def f():\n    return 1\n"),
    ("match", "def f(x):\n    match x:\n        case 1:\n            return 1\n"
              "        case _:\n            return 0\n"),
    ("walrus", "def f(data):\n    if (n := len(data)) > 1:\n        return n\n"),
    ("global", "def f():\n    global X\n    X = 1\n"),
    ("raise", "def f(x):\n    if not x:\n        raise ValueError('no')\n"),
    ("return-none", "def f():\n    return None\n"),
    ("boolean-const", "def f(x):\n    return x is True and x is not False\n"),
    ("chained-compare", "def f(a, b, c):\n    return a < b < c\n"),
    ("star-args", "def f(*args, **kwargs):\n    return len(args) + len(kwargs)\n"),
    ("nested-if", "def f(a, b):\n    if a:\n        if b:\n            return 1\n"),
    ("for-else", "def f(items):\n    for item in items:\n        if item:\n"
                 "            break\n    else:\n        return 0\n"),
    ("except-multiple", "def f():\n    try:\n        pass\n    except (TypeError, ValueError):\n"
                        "        pass\n"),
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fetch() -> Path:
    ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
    if ARCHIVE.exists() and digest(ARCHIVE) == PIN["sha256"]:
        return ARCHIVE
    with urllib.request.urlopen(PIN["url"], timeout=60) as response:  # noqa: S310 - pinned https URL
        payload = response.read()
    found = hashlib.sha256(payload).hexdigest()
    if found != PIN["sha256"]:
        raise SystemExit(f"pinned sdist hash mismatch: {found} != {PIN['sha256']}")
    ARCHIVE.write_bytes(payload)
    return ARCHIVE


def unpack(archive: Path) -> None:
    """Unpack `mccabe.py` and its tests into the oracle tree."""
    with tarfile.open(archive) as tar:
        wanted = {
            "mccabe-0.7.0/mccabe.py": ORACLE / "mccabe.py",
            "mccabe-0.7.0/test_mccabe.py": ORACLE / "test_mccabe.py",
        }
        for member_name, destination in wanted.items():
            member = tar.getmember(member_name)
            handle = tar.extractfile(member)
            if handle is None:
                raise SystemExit(f"cannot read {member_name}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(handle.read())


def oracle_tests() -> int:
    """Count the pinned oracle's tests, without running them (CI counts them)."""
    path = ORACLE / "test_mccabe.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    )


def suite_inputs() -> list[str]:
    """The source snippets the target's own tests use, as inputs.

    Read from `test_mccabe.py`'s triple-quoted constants. This is the *inputs* only:
    the asserted complexity numbers never leave the suite, so a case set built here
    cannot inherit the oracle's answers.
    """
    text = (ORACLE / "test_mccabe.py").read_text(encoding="utf-8")
    snippets = re.findall(r'"""(.*?)"""', text, flags=re.S)
    return [snippet for snippet in snippets if "def " in snippet or "class " in snippet]


def build_cases() -> list[Case]:
    cases: list[Case] = []
    seen: set[str] = set()
    for name, source in SNIPPETS:
        code = hashlib.sha256(source.encode("utf-8")).hexdigest()[:8]
        case_id = f"fixture-{name}-{code}"
        if case_id in seen:
            continue
        seen.add(case_id)
        cases.append(
            Case(
                id=case_id,
                source="snippets",
                entry="get_code_complexity",
                args=(source,),
                projection="",
            )
        )
    # The same inputs through the migration's surface: `main` is what a
    # `mig-manual-argparse` patch rewrites, so its behaviour needs a case kind that
    # can see a program. These are CLI cases, with the same seeds as inputs.
    for name, source in (
        ("exit-zero", "def f():\n    return 1\n"),
        ("exit-nonzero", "def f(x):\n    if x:\n        if x > 1:\n            return 1\n"),
        ("stdin", "def f():\n    pass\n"),
    ):
        cases.append(
            Case(
                id=f"probe-cli-{name}",
                source="probes",
                entry="mccabe.py",
                args=(),
                kind="cli",
                files={"sample.py": source},
                argv=() if name == "stdin" else ("sample.py",),
                stdin=source if name == "stdin" else "",
            )
        )
    cases.append(
        Case(
            id="api-module",
            source="api",
            entry="mccabe",
            args=(),
            kind="api",
            module="mccabe",
        )
    )
    return cases


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify pins and the case set")
    args = parser.parse_args(argv)

    if not args.check:
        archive = fetch()
        print(f"archive {ARCHIVE.name} sha256 {digest(archive)[:16]}")
        unpack(archive)
    elif not (ORACLE / "test_mccabe.py").exists():
        # `--check` on a fresh clone still needs the oracle to count its tests, so it
        # fetches and unpacks the pinned artifact. It never writes the committed case
        # set: that is what it is checking.
        archive = fetch()
        unpack(archive)

    if digest(ARCHIVE) != PIN["sha256"]:
        raise SystemExit("the pinned archive does not match PIN")
    tests = oracle_tests()

    cases = build_cases()
    payload = {
        "target": "mccabe",
        "case_set_hash": case_set_hash(cases),
        "counts_by_source": {
            source: sum(1 for case in cases if case.source == source)
            for source in sorted({case.source for case in cases})
        },
        "oracle_tests": tests,
        "oracle": {
            "name": PIN["name"],
            "version": PIN["version"],
            "sha256": PIN["sha256"],
            "tests": tests,
        },
        "cases": [case.body() for case in cases],
    }
    if args.check and CASES.exists():
        existing = json.loads(CASES.read_text(encoding="utf-8"))
        if existing.get("case_set_hash") != payload["case_set_hash"]:
            raise SystemExit(
                "the committed case set is stale: "
                f"{existing.get('case_set_hash')} != {payload['case_set_hash']}"
            )
    if not args.check:
        CASES.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        RULES.write_text(
            json.dumps(
                {
                    "target": "mccabe",
                    "case_set_hash": payload["case_set_hash"],
                    "notes": (
                        "No masking: every case seeds the target with a string literal, and "
                        "the shim fixes clock, hash seed, locale and cwd, so an A/A pass over "
                        "the case set is the noise measurement."
                    ),
                    "rules": [],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        APPROVALS.write_text(
            json.dumps(
                {
                    "schema": "harness-approvals/1",
                    "target": "mccabe 0.7.0",
                    "note": (
                        "No approved differences yet: mccabe has not been migrated. A "
                        "migration's output is approved per run by a human, bound to the "
                        "sha256 of the observed diff, never pre-written here."
                    ),
                    "approvals": [],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    print(
        f"mccabe {PIN['version']} sha256 {PIN['sha256'][:16]} | "
        f"{len(cases)} cases hash {payload['case_set_hash'][:12]} | "
        f"oracle {tests} tests | sources {payload['counts_by_source']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
