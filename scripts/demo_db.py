"""One place that decides which database a DEMO may write to.

The checkpoint-9 incident: `scripts/sandbox_demo.py` and the test suite both
defaulted to `app_test`, so running `pytest` after a chaos run truncated the
very rows the run had just produced (the `clean_db` fixture). The evidence
survived only as the committed JSON. That is a measurement bug, not a test bug:
a demo's output must survive a test run.

Rule, enforced here rather than by convention:

- the demo database comes from `ENGINE_DEMO_DATABASE_URL`, falling back to
  `DATABASE_URL` only so the containerised runners keep working;
- its name must end in `_demo` — a `_test` database is refused outright, which
  also covers an explicit `ENGINE_DEMO_DATABASE_URL=...app_test`;
- normalisation ignores credentials and treats `localhost` and
  `host.docker.internal` as the same host, so a demo cannot reach the test
  database through the docker gateway.

Every demo still writes its JSON artifact into `docs/results/` at the end of
the run (that was already true); this module is only about which database it
may touch while running.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

_DEMO_SUFFIX = "_demo"
_TEST_SUFFIX = "_test"


def _hosts(url: str) -> set[str]:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    port = parts.port or 5432
    aliases = {host}
    if host == "localhost":
        aliases.add("host.docker.internal")
        aliases.add("127.0.0.1")
    if host in {"host.docker.internal", "127.0.0.1"}:
        aliases |= {"localhost", "host.docker.internal", "127.0.0.1"}
    return {f"{alias}:{port}" for alias in aliases}


def resolve_demo_url(script: str) -> str:
    """The database URL a demo may use. Exits rather than guessing."""
    url = (
        os.environ.get("ENGINE_DEMO_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
    ).strip()
    if not url:
        raise SystemExit(
            f"{script}: a demo needs ENGINE_DEMO_DATABASE_URL (or DATABASE_URL). "
            "There is no default: a demo that inherits the test database would "
            "have its evidence truncated by the next pytest run."
        )
    parts = urlsplit(url)
    dbname = parts.path.lstrip("/")
    if dbname.endswith(_TEST_SUFFIX):
        raise SystemExit(
            f"{script}: refusing to run against {dbname!r} — test databases are "
            f"truncated by pytest's clean_db fixture. Use a {_DEMO_SUFFIX} name."
        )
    if not dbname.endswith(_DEMO_SUFFIX):
        raise SystemExit(
            f"{script}: refusing database {dbname!r}: a demo database name must "
            f"end with {_DEMO_SUFFIX!r}, so it can never collide with a "
            f"{_TEST_SUFFIX!r} database."
        )
    test_url = os.environ.get("TEST_DATABASE_URL", "").strip()
    if test_url and urlsplit(test_url).path.lstrip("/") == dbname:
        if _hosts(test_url) & _hosts(url):
            raise SystemExit(
                f"{script}: {dbname!r} on this host IS the test database "
                "(TEST_DATABASE_URL); demos write evidence, pytest truncates it."
            )
    return url


def redacted(url: str) -> str:
    """host:port/dbname, safe to print and to put in a report."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname}:{parts.port}{parts.path}"


__all__ = ["resolve_demo_url", "redacted"]
