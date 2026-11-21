"""Per-target oracle sanity check, before any mutation number is quoted.

A second oracle is only worth having if it is alive. For every target this script
asserts the two things that make the upstream suite usable at all:

  1. on the **unmutated** tree the suite passes (a suite that is red to begin with
     would "kill" every mutant and inflate the rate);
  2. a **known-broken** mutant fails it (a suite that cannot fail would let real
     bugs through and deflate it).

It also records a fact worth knowing before trusting an sdist: `pycodestyle`
ships its `tests/` but not the `testing/` package those tests import, so the
**sdist** suite cannot run at all. The pinned GitHub tree at the release tag can,
and that is what the oracle uses.

Usage:
    python scripts/oracle_canary.py            # every pinned target
    python scripts/oracle_canary.py --target parse
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path
from types import ModuleType
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

TARGETS: dict[str, dict[str, Any]] = {
    "parse": {
        "url": "https://files.pythonhosted.org/packages/source/p/parse/parse-1.22.4.tar.gz",
        "sha256": "706d458feed5a81b02768d3156e8070baf6cee7d150a3a3c0cd43101a875c3b5",
        "module": "parse/__init__.py",
        "tests": "tests",
        "archive": ".harness/sdist/parse-1.22.4.tar.gz",
        "root": ".harness/oracle/parse",
        "strip": 1,
        # `tz[1:]` -> `tz[2:]` in timezone-offset parsing: every `{:ti}`-style case
        # with a `+HH:MM` offset fails, on any platform. Picking "the first
        # `compare_swap`" instead flipped `sys.platform == 'win32'`, which one
        # platform's suite notices and the other's does not — a canary whose
        # meaning changes with the host.
        "broken_site": "slice_bound@254:30",
    },
    "pycodestyle": {
        # The sdist cannot be used: its tests import `testing.support`, which is
        # not shipped. The tree at the release tag is the runnable one.
        "url": "https://codeload.github.com/PyCQA/pycodestyle/tar.gz/refs/tags/2.15.0",
        "sha256": "3cb6afba1667f13b4ca7884154299108ef8e633a36526e29412a5cec7b5db4bd",
        "module": "pycodestyle.py",
        "tests": "tests",
        "archive": ".harness/candidates/pycodestyle-git-2.15.0.tar.gz",
        "root": ".harness/oracle/pycodestyle",
        "strip": 1,
        # `MAX_LINE_LENGTH = 79` -> 80. The suite asserts E501 on an 80-character
        # line, so this must be killed everywhere, by a one-token edit.
        "broken_site": "int_plus@81:18",
    },
}


def _mutate_script() -> ModuleType:
    path = REPO / "scripts" / "mutate.py"
    spec = importlib.util.spec_from_file_location("oracle_canary_mutate", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def fetch(name: str) -> Path:
    spec = TARGETS[name]
    archive = REPO / spec["archive"]
    if not (archive.exists() and _sha(archive) == spec["sha256"]):
        archive.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(spec["url"], timeout=120) as response:
            payload = response.read()
        if hashlib.sha256(payload).hexdigest() != spec["sha256"]:
            raise SystemExit(f"{name}: pinned hash mismatch")
        archive.write_bytes(payload)
    root: Path = REPO / str(spec["root"])
    if not (root / str(spec["module"])).exists():
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        with tarfile.open(archive) as tar:
            tar.extractall(root, filter="data")
        member = root / spec["module"]
        if not member.exists():
            # archives with a leading directory component
            inner = [p for p in root.iterdir() if p.is_dir()]
            if len(inner) == 1:
                for item in inner[0].iterdir():
                    shutil.move(str(item), str(root / item.name))
                inner[0].rmdir()
    return root


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(name: str) -> dict[str, Any]:
    from harness.mutate import enumerate_sites, mutate

    module = _mutate_script()
    root = fetch(name)
    spec = TARGETS[name]
    work = REPO / ".harness" / "oracle-canary" / name
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    clean = work / "clean"
    shutil.copytree(root, clean, ignore=shutil.ignore_patterns(".git", "__pycache__"))

    clean_killed, clean_note, _ = module._run_tests(clean / spec["tests"], clean)
    source = (root / spec["module"]).read_text(encoding="utf-8")
    # A named site, not "the first of some operator": a canary must not change
    # meaning when the target or the mutator changes, and it must be asserted to
    # exist so a target update fails here instead of silently picking a benign one.
    wanted = str(spec["broken_site"])
    site = next((s for s in enumerate_sites(source) if s.key == wanted), None)
    if site is None:
        raise SystemExit(f"{name}: canary site {wanted} no longer exists")
    broken = work / "broken"
    shutil.copytree(root, broken, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    (broken / spec["module"]).write_text(mutate(source, site), encoding="utf-8")
    broken_killed, broken_note, _ = module._run_tests(broken / spec["tests"], broken)

    return {
        "target": name,
        "mutant": site.key,
        "clean_suite": "passes" if not clean_killed else "FAILED",
        "clean_note": clean_note,
        "broken_mutant": "killed" if broken_killed else "SURVIVED",
        "broken_note": broken_note,
        "ok": (not clean_killed) and broken_killed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", action="append", default=[])
    parser.add_argument("--out", default="docs/results/oracle-canary-20261120.json")
    args = parser.parse_args(argv)
    names = args.target or list(TARGETS)
    results = [check(name) for name in names]
    for row in results:
        print(
            f"{row['target']:12} clean: {row['clean_suite']:6} ({row['clean_note']}) "
            f"| {row['mutant']} {row['broken_mutant']} ({row['broken_note']})"
        )
    (REPO / args.out).write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    return 0 if all(row["ok"] for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
