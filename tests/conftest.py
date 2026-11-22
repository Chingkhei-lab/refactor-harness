"""Shared fixtures: guarded test database, migrations, and truncation."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from engine.config import Settings, load_settings

_REPO_ROOT = Path(__file__).resolve().parents[1]

_DEFAULT_TEST_URL = "postgresql://app:app@localhost:5433/app_test"


def _database_url() -> str:
    url = (
        os.environ.get("TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or _DEFAULT_TEST_URL
    )
    dbname = urlsplit(url).path.lstrip("/")
    if not dbname.endswith("_test") and os.environ.get("CI") not in {"1", "true"}:
        raise RuntimeError(
            f"Refusing to run tests against database {dbname!r}: outside CI the "
            "database name must end with '_test' so a wrong DSN can never wipe "
            "real data."
        )
    return url


@pytest.fixture(scope="session")
def settings() -> Settings:
    """Run migrations once, then hand out short-lease settings for tests."""
    url = _database_url()
    os.environ["ALEMBIC_DATABASE_URL"] = url
    alembic_config = Config(str(_REPO_ROOT / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    command.upgrade(alembic_config, "head")
    return replace(
        load_settings(),
        database_url=url,
        lease_seconds=2.0,
        heartbeat_interval_seconds=2.0 / 3,
        statement_timeout_ms=30000,
    )


@pytest.fixture
def clean_db(settings: Settings) -> Settings:
    """Truncate queue, workflow, and workspace tables before and after."""
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute(
            "TRUNCATE jobs, workflows, tool_calls, workspace_patches, "
            "sandbox_runs, workspaces, blobs, llm_calls, agent_runs CASCADE"
        )
    try:
        yield settings
    finally:
        with psycopg.connect(settings.database_url, autocommit=True) as conn:
            conn.execute(
                "TRUNCATE jobs, workflows, tool_calls, workspace_patches, "
                "workspaces, blobs, llm_calls, agent_runs CASCADE"
            )
