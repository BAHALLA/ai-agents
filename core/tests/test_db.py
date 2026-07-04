"""Tests for session-store helpers (orrery_core/db.py).

Only PostgreSQL is supported; SQLite was removed. Tests needing a live database
use the ``postgres_url`` fixture (skips when none is reachable). Reachability
and fallback paths are exercised with an unreachable PostgreSQL URL, which needs
no server.
"""

from __future__ import annotations

import pytest
from google.adk.sessions.database_session_service import DatabaseSessionService
from google.adk.sessions.in_memory_session_service import InMemorySessionService

from orrery_core.persistence.db import (
    DatabaseUnavailableError,
    create_session_service,
    database_reachable,
    is_postgres_url,
    to_async_url,
    to_sync_url,
)

# A PostgreSQL URL whose host/port refuses connections — stands in for an
# unreachable database without needing a live server.
_UNREACHABLE_PG = "postgresql://user:pass@127.0.0.1:1/none"


def test_is_postgres_url():
    assert is_postgres_url("postgresql+asyncpg://u:p@h/db") is True
    assert is_postgres_url("postgres://u:p@h/db") is True
    assert is_postgres_url("sqlite:///x.db") is False


def test_to_sync_url_normalizes_async_postgres():
    assert to_sync_url("postgresql+asyncpg://u:p@h/db") == "postgresql+psycopg2://u:p@h/db"
    assert to_sync_url("postgresql://u:p@h/db") == "postgresql://u:p@h/db"


def test_to_async_url_normalizes_to_asyncpg():
    assert to_async_url("postgresql://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"
    assert to_async_url("postgresql+psycopg2://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"
    # Already-async URLs are left untouched.
    assert to_async_url("postgresql+asyncpg://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"


# ── Reachability ─────────────────────────────────────────────────────


def test_database_reachable_true_for_live_postgres(postgres_url):
    assert database_reachable(postgres_url) is True


def test_database_reachable_false_for_unreachable(caplog):
    with caplog.at_level("WARNING", logger="orrery.db"):
        assert database_reachable(_UNREACHABLE_PG) is False
    assert any("Database unreachable" in r.message for r in caplog.records)


# ── Session service factory ──────────────────────────────────────────


def test_create_session_service_persistent_when_reachable(postgres_url):
    svc = create_session_service(postgres_url)
    assert isinstance(svc, DatabaseSessionService)


def test_create_session_service_in_memory_when_no_url():
    assert isinstance(create_session_service(None), InMemorySessionService)


def test_create_session_service_rejects_non_postgres(monkeypatch):
    """A non-PostgreSQL URL is a misconfiguration — fail fast by default."""
    monkeypatch.delenv("ORRERY_DB_ALLOW_INMEMORY_FALLBACK", raising=False)
    with pytest.raises(DatabaseUnavailableError, match="only PostgreSQL is supported"):
        create_session_service("sqlite:///whatever.db")


def test_create_session_service_raises_when_unreachable_by_default(monkeypatch):
    """A configured-but-unreachable DB fails fast so pods CrashLoopBackOff."""
    monkeypatch.delenv("ORRERY_DB_ALLOW_INMEMORY_FALLBACK", raising=False)
    with pytest.raises(DatabaseUnavailableError, match="database unreachable"):
        create_session_service(_UNREACHABLE_PG)


def test_create_session_service_falls_back_when_fallback_env_set(monkeypatch, caplog):
    """ORRERY_DB_ALLOW_INMEMORY_FALLBACK opts into the in-memory fallback."""
    monkeypatch.setenv("ORRERY_DB_ALLOW_INMEMORY_FALLBACK", "1")
    with caplog.at_level("WARNING", logger="orrery.db"):
        svc = create_session_service(_UNREACHABLE_PG)
    assert isinstance(svc, InMemorySessionService)
    assert any("falling back to in-memory sessions" in r.message for r in caplog.records)


def test_create_session_service_falls_back_when_allow_fallback_arg():
    """The explicit allow_fallback argument overrides the env default."""
    svc = create_session_service(_UNREACHABLE_PG, allow_fallback=True)
    assert isinstance(svc, InMemorySessionService)
