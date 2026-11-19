"""The demo/test database split (checkpoint 9 review round 2).

The incident these tests pin: demos and pytest shared `app_test`, so a test
run truncated a chaos run's evidence. The rule now lives in
`scripts/demo_db.py` and refuses at startup instead of relying on discipline.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "demo_db.py"
_spec = importlib.util.spec_from_file_location("demo_db", _SCRIPT)
assert _spec and _spec.loader
demo_db = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demo_db)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ENGINE_DEMO_DATABASE_URL", "DATABASE_URL", "TEST_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)


def test_no_default_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit, match="ENGINE_DEMO_DATABASE_URL"):
        demo_db.resolve_demo_url("demo")


def test_test_database_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENGINE_DEMO_DATABASE_URL", "postgresql://app:app@localhost:5433/app_test")
    with pytest.raises(SystemExit, match="test databases are truncated"):
        demo_db.resolve_demo_url("demo")


def test_random_name_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENGINE_DEMO_DATABASE_URL", "postgresql://app:app@localhost:5433/prod")
    with pytest.raises(SystemExit, match="must .*end with '_demo'"):
        demo_db.resolve_demo_url("demo")


def test_demo_database_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENGINE_DEMO_DATABASE_URL", "postgresql://app:app@localhost:5433/chaos_demo")
    assert demo_db.resolve_demo_url("demo") == "postgresql://app:app@localhost:5433/chaos_demo"


def test_databse_url_fallback_still_refuses_test(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://app:app@host.docker.internal:5433/app_test")
    with pytest.raises(SystemExit, match="test databases are truncated"):
        demo_db.resolve_demo_url("demo")


def test_redacted_hides_credentials() -> None:
    assert demo_db.redacted("postgresql://app:secret@db:5433/x_demo") == "postgresql://db:5433/x_demo"
