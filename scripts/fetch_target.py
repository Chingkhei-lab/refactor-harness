"""Fetch the pinned target sdist, verified by hash, and unpack it.

The target used to live in a git-ignored `.harness/` directory, so CI could not
run anything that needed it: the mutator test skipped, and the sandbox gate had
no source to mutate. This makes the target reproducible from the published
artifact: the URL is pinned, the sha256 is pinned, and a mismatch is a hard
failure (never "download and hope").

Usage:
    python scripts/fetch_target.py                # .harness/target + .harness/sdist
    python scripts/fetch_target.py --check        # verify only, do not unpack
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import tarfile
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PIN = {
    "name": "parse",
    "version": "1.22.4",
    "url": "https://files.pythonhosted.org/packages/source/p/parse/parse-1.22.4.tar.gz",
    "sha256": "706d458feed5a81b02768d3156e8070baf6cee7d150a3a3c0cd43101a875c3b5",
}


def fetch(dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and _digest(dest) == PIN["sha256"]:
        return dest
    with urllib.request.urlopen(PIN["url"], timeout=60) as response:
        payload = response.read()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != PIN["sha256"]:
        raise SystemExit(
            f"pinned sdist hash mismatch: got {digest}, expected {PIN['sha256']}"
        )
    dest.write_bytes(payload)
    return dest


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def unpack(archive: Path, target: Path) -> None:
    """Extract the sdist's package into `target`, refusing hostile members."""
    with tarfile.open(archive) as tar:
        members = [m for m in tar.getmembers() if m.name.endswith("/parse/__init__.py")]
        if not members:
            raise SystemExit("no parse/__init__.py in the sdist")
        member = members[0]
        data = tar.extractfile(member)
        if data is None:
            raise SystemExit("cannot read parse/__init__.py from the sdist")
        target.mkdir(parents=True, exist_ok=True)
        (target / "__init__.py").write_bytes(data.read())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify the archive only")
    parser.add_argument("--target", default=".harness/target/parse")
    parser.add_argument("--archive", default=".harness/sdist/parse-1.22.4.tar.gz")
    args = parser.parse_args(argv)

    archive = fetch(REPO / args.archive)
    if args.check:
        print(f"ok {PIN['name']} {PIN['version']} {PIN['sha256'][:16]}…")
        return 0
    unpack(archive, REPO / args.target)
    print(f"unpacked {PIN['name']} {PIN['version']} -> {args.target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
