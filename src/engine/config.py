"""Engine configuration, loaded from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    """Tunables for the queue and its workers."""

    database_url: str
    lease_seconds: float
    heartbeat_interval_seconds: float
    max_attempts: int
    backoff_base_seconds: float
    backoff_cap_seconds: float
    reaper_interval_seconds: float
    reaper_batch: int
    idle_sleep_min_seconds: float
    idle_sleep_max_seconds: float
    connect_timeout_seconds: int
    statement_timeout_ms: int


def load_settings() -> Settings:
    lease_seconds = _env_float("ENGINE_LEASE_SECONDS", 30.0)
    return Settings(
        database_url=os.environ.get(
            "DATABASE_URL", "postgresql://app:app@localhost:5433/app"
        ),
        lease_seconds=lease_seconds,
        heartbeat_interval_seconds=lease_seconds / 3,
        max_attempts=_env_int("ENGINE_MAX_ATTEMPTS", 5),
        backoff_base_seconds=_env_float("ENGINE_BACKOFF_BASE_SECONDS", 1.0),
        backoff_cap_seconds=_env_float("ENGINE_BACKOFF_CAP_SECONDS", 60.0),
        reaper_interval_seconds=_env_float("ENGINE_REAPER_INTERVAL_SECONDS", 5.0),
        reaper_batch=_env_int("ENGINE_REAPER_BATCH", 100),
        idle_sleep_min_seconds=_env_float("ENGINE_IDLE_SLEEP_MIN_SECONDS", 0.1),
        idle_sleep_max_seconds=_env_float("ENGINE_IDLE_SLEEP_MAX_SECONDS", 1.0),
        connect_timeout_seconds=_env_int("ENGINE_CONNECT_TIMEOUT_SECONDS", 5),
        statement_timeout_ms=_env_int("ENGINE_STATEMENT_TIMEOUT_MS", 30000),
    )
