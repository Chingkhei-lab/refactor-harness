"""Pin the oracle tree and prove it is the same module as the sdist.

A second oracle is only an oracle for the code under test if the tree it runs
belongs to the same release. `pycodestyle` 2.15.0 ships two artifacts that are not
the same tarball:

* the **PyPI sdist** — the module under test, and the only artifact a user
  installs; it also ships `tests/` but NOT the `testing/` package those tests
  import, so its suite cannot run (8 collection errors);
* the **GitHub tag tarball** — the runnable suite.

So the oracle comes from the tag tarball while the module comes from the sdist.
That is only sound if the module file is byte-identical in both. This script
checks exactly that, and pins both by sha256:

    python scripts/pin_oracle.py

Exit status is non-zero if either hash is not the pinned one or the module files
differ, so CI fails rather than comparing two releases by accident.
"""

from __future__ import annotations

import hashlib
import json
import tarfile
import urllib.request
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "harness" / "oracle" / "pycodestyle.json"

Target = dict[str, dict[str, str]]

TARGET: dict[str, Any] = {
    "name": "pycodestyle",
    "version": "2.15.0",
    # The module under test, and the sdist a user gets.
    "sdist": {
        "url": "https://files.pythonhosted.org/packages/source/p/pycodestyle/pycodestyle-2.15.0.tar.gz",
        "sha256": "",
        "module": "pycodestyle-2.15.0/pycodestyle.py",
        "cache": ".harness/candidates/pycodestyle-2.15.0.tar.gz",
        "pin": "318f5db083869b4c4dad922d0b11124fb27ab181b6730b93371da671e31bd50e",
    },
    # The runnable suite: the tag tarball carries `testing/`.
    "tests": {
        "url": "https://codeload.github.com/PyCQA/pycodestyle/tar.gz/refs/tags/2.15.0",
        "sha256": "",
        "module": "pycodestyle-2.15.0/pycodestyle.py",
        "cache": ".harness/candidates/pycodestyle-git-2.15.0.tar.gz",
        "pin": "3cb6afba1667f13b4ca7884154299108ef8e633a36526e29412a5cec7b5db4bd",
    },
}


def _fetch(spec: dict[str, str]) -> bytes:
    cache = REPO / spec["cache"]
    if cache.exists():
        return cache.read_bytes()
    cache.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(spec["url"], timeout=120) as response:
        payload = bytes(response.read())
    cache.write_bytes(payload)
    return payload


def _member(payload: bytes, name: str) -> bytes:
    import io

    with tarfile.open(fileobj=io.BytesIO(payload)) as tar:
        member = tar.getmember(name)
        handle = tar.extractfile(member)
        assert handle is not None
        return handle.read()


def main() -> int:
    report: dict[str, object] = {"target": TARGET["name"], "version": TARGET["version"]}
    modules: dict[str, str] = {}
    digests: dict[str, str] = {}
    detail: dict[str, dict[str, str]] = {}
    for key in ("sdist", "tests"):
        spec: dict[str, str] = TARGET[key]
        payload = _fetch(spec)
        digest = hashlib.sha256(payload).hexdigest()
        digests[key] = digest
        if digest != spec["pin"]:
            print(f"{key}: hash {digest} is not the pinned {spec['pin']}")
            return 1
        modules[key] = hashlib.sha256(_member(payload, spec["module"])).hexdigest()
        detail[key] = {
            "sha256": digest,
            "url": str(spec["url"]),
            "module_sha256": modules[key],
        }
    report.update(detail)

    identical = modules["sdist"] == modules["tests"]
    report["module_identical"] = identical
    report["runnable_suite"] = "tests"
    report["note"] = (
        "The suite comes from the tag tarball because the sdist ships tests/ "
        "without the testing/ package they import. The module files are compared "
        "byte for byte: if they ever differ, the oracle is testing a different "
        "release and the numbers are void."
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"sdist sha256 {digests['sdist'][:12]}, "
        f"tests sha256 {digests['tests'][:12]}, "
        f"module sha256 {modules['sdist'][:16]} identical={identical}"
    )
    return 0 if identical else 1


if __name__ == "__main__":
    raise SystemExit(main())
