"""Tests for the persistent memory backend (DatabaseMemoryService).

Persistence tests run against PostgreSQL (the only supported database) and skip
when none is reachable — see the ``postgres_url`` / ``pg_app`` fixtures in
conftest. Fallback/validation tests need no live server.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from google.adk.events import Event
from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
from google.adk.sessions.session import Session
from google.genai import types

from orrery_core.persistence.db import DatabaseUnavailableError
from orrery_core.persistence.memory import (
    DatabaseMemoryService,
    SecureMemoryService,
    _to_sync_url,
    create_memory_service,
)

# A PostgreSQL URL whose host/port refuses connections — stands in for an
# unreachable database without needing a live server.
_UNREACHABLE_PG = "postgresql://user:pass@127.0.0.1:1/none"


def _make_event(text: str, event_id: str = "evt-1", author: str = "user") -> Event:
    return Event(
        id=event_id,
        author=author,
        content=types.Content(role="user", parts=[types.Part.from_text(text=text)]),
    )


def _make_session(
    events: list[Event],
    app_name: str,
    user_id: str = "test_user",
    session_id: str = "sess-1",
) -> Session:
    session = MagicMock(spec=Session)
    session.app_name = app_name
    session.user_id = user_id
    session.id = session_id
    session.events = events
    return session


def _text(mem) -> str:
    return mem.content.parts[0].text


# ── URL normalization ────────────────────────────────────────────────


def test_to_sync_url_normalizes_async_postgres():
    assert _to_sync_url("postgresql+asyncpg://u:p@h/db") == "postgresql+psycopg2://u:p@h/db"
    # Already-sync URLs pass through untouched.
    assert _to_sync_url("postgresql://u:p@h/db") == "postgresql://u:p@h/db"


# ── Durability (the whole point) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_survives_new_service_instance(pg_app):
    """A fresh service on the same DB recalls what a prior instance stored."""
    url, app = pg_app
    writer = DatabaseMemoryService(db_url=url)
    await writer.add_session_to_memory(
        _make_session([_make_event("Kafka broker down in us-east-1", "e1")], app)
    )

    # Simulate a restart: brand-new instance, same database.
    reader = DatabaseMemoryService(db_url=url)
    result = await reader.search_memory(app_name=app, user_id="test_user", query="Kafka")

    assert len(result.memories) == 1
    assert "Kafka" in _text(result.memories[0])


@pytest.mark.asyncio
async def test_search_keyword_and_user_scoping(pg_app):
    url, app = pg_app
    svc = DatabaseMemoryService(db_url=url)
    await svc.add_session_to_memory(
        _make_session([_make_event("User A incident", "e1")], app, user_id="user_a")
    )
    await svc.add_session_to_memory(
        _make_session([_make_event("User B incident", "e2")], app, user_id="user_b")
    )

    a = await svc.search_memory(app_name=app, user_id="user_a", query="incident")
    assert len(a.memories) == 1
    assert "User A" in _text(a.memories[0])

    none = await svc.search_memory(app_name=app, user_id="user_a", query="database")
    assert len(none.memories) == 0


@pytest.mark.asyncio
async def test_add_session_is_idempotent(pg_app):
    """Re-adding the same session replaces its events (no duplicates)."""
    url, app = pg_app
    svc = DatabaseMemoryService(db_url=url)
    session = _make_session([_make_event("recurring event", "e1")], app)

    await svc.add_session_to_memory(session)
    await svc.add_session_to_memory(session)

    result = await svc.search_memory(app_name=app, user_id="test_user", query="recurring")
    assert len(result.memories) == 1


@pytest.mark.asyncio
async def test_add_events_dedups_by_id(pg_app):
    """add_events_to_memory treats events as a delta and skips known IDs."""
    url, app = pg_app
    svc = DatabaseMemoryService(db_url=url)
    await svc.add_events_to_memory(
        app_name=app, user_id="test_user", events=[_make_event("alpha", "e1")]
    )
    await svc.add_events_to_memory(
        app_name=app,
        user_id="test_user",
        events=[_make_event("alpha", "e1"), _make_event("beta", "e2")],
    )

    result = await svc.search_memory(app_name=app, user_id="test_user", query="alpha beta")
    assert len(result.memories) == 2


@pytest.mark.asyncio
async def test_timestamp_round_trips_as_iso(pg_app):
    url, app = pg_app
    svc = DatabaseMemoryService(db_url=url)
    event = _make_event("timestamped", "e1")
    event.timestamp = 1_700_000_000.0
    await svc.add_session_to_memory(_make_session([event], app))

    result = await svc.search_memory(app_name=app, user_id="test_user", query="timestamped")
    assert result.memories[0].timestamp is not None
    assert "T" in result.memories[0].timestamp  # ISO 8601, not a raw float


# ── Factory + redaction integration ──────────────────────────────────


@pytest.mark.asyncio
async def test_create_memory_service_persists_and_redacts(pg_app):
    """The factory wraps the DB backend so secrets never hit persistent storage."""
    url, app = pg_app
    svc = create_memory_service(db_url=url)
    assert isinstance(svc, SecureMemoryService)

    await svc.add_session_to_memory(
        _make_session([_make_event("Config password=hunter2 here", "e1")], app)
    )

    # Read back through a *fresh* DB-backed service — the raw secret must be gone.
    reader = DatabaseMemoryService(db_url=url)
    result = await reader.search_memory(app_name=app, user_id="test_user", query="Config")
    assert len(result.memories) == 1
    text = _text(result.memories[0])
    assert "hunter2" not in text
    assert "[REDACTED]" in text


def test_create_memory_service_uses_env_database_url(monkeypatch, postgres_url):
    monkeypatch.setenv("DATABASE_URL", postgres_url)
    svc = create_memory_service()
    assert isinstance(svc._inner, DatabaseMemoryService)


# ── In-memory / rejection / fallback (no live server needed) ─────────


def test_create_memory_service_falls_back_to_in_memory(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    svc = create_memory_service()
    assert isinstance(svc, SecureMemoryService)
    assert isinstance(svc._inner, InMemoryMemoryService)


def test_create_memory_service_rejects_non_postgres(monkeypatch):
    """A non-PostgreSQL URL (e.g. SQLite) fails fast by default."""
    monkeypatch.delenv("ORRERY_DB_ALLOW_INMEMORY_FALLBACK", raising=False)
    with pytest.raises(DatabaseUnavailableError, match="only PostgreSQL is supported"):
        create_memory_service(db_url="sqlite:///whatever.db")


def test_create_memory_service_raises_when_db_unreachable(monkeypatch):
    """An unreachable PostgreSQL fails fast, mirroring the session store."""
    monkeypatch.delenv("ORRERY_DB_ALLOW_INMEMORY_FALLBACK", raising=False)
    with pytest.raises(DatabaseUnavailableError, match="PostgreSQL memory store unavailable"):
        create_memory_service(db_url=_UNREACHABLE_PG)


def test_create_memory_service_falls_back_when_fallback_env_set(monkeypatch, caplog):
    """ORRERY_DB_ALLOW_INMEMORY_FALLBACK opts into the in-memory fallback."""
    monkeypatch.setenv("ORRERY_DB_ALLOW_INMEMORY_FALLBACK", "1")
    with caplog.at_level("WARNING", logger="orrery.memory"):
        svc = create_memory_service(db_url=_UNREACHABLE_PG)
    assert isinstance(svc, SecureMemoryService)
    assert isinstance(svc._inner, InMemoryMemoryService)
    assert any("PostgreSQL memory store unavailable" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_service_usable_after_fallback(monkeypatch):
    """After falling back (opt-in), the service still stores and recalls."""
    monkeypatch.setenv("ORRERY_DB_ALLOW_INMEMORY_FALLBACK", "1")
    svc = create_memory_service(db_url=_UNREACHABLE_PG)
    await svc.add_session_to_memory(_make_session([_make_event("still works", "e1")], "app_x"))
    result = await svc.search_memory(app_name="app_x", user_id="test_user", query="works")
    assert len(result.memories) == 1
