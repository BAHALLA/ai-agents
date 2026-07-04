"""Memory services: a secure redacting wrapper and a persistent backend.

Two pieces work together:

- :class:`SecureMemoryService` — a wrapper that redacts secrets at write time
  and bounds per-save storage, then **delegates** to any inner
  :class:`BaseMemoryService`. The inner service is swappable.
- :class:`DatabaseMemoryService` — a persistent inner backend that stores
  long-term memory in PostgreSQL so cross-session recall survives restarts and
  is shared across replicas. It mirrors ADK's ``InMemoryMemoryService``
  keyword-matching semantics.

Use :func:`create_memory_service` to assemble the two from a database URL::

    from orrery_core.memory import create_memory_service

    # Persistent (PostgreSQL), redacted:
    memory = create_memory_service(db_url="postgresql+asyncpg://…/agents")
    # Falls back to in-memory when no db_url and no DATABASE_URL is set.
    runner = Runner(app=app, session_service=..., memory_service=memory)
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from google.adk.events import Event
from google.adk.memory.base_memory_service import BaseMemoryService, SearchMemoryResponse
from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
from google.adk.memory.memory_entry import MemoryEntry
from google.adk.sessions.session import Session
from google.genai import types
from sqlalchemy.exc import SQLAlchemyError

from ..observability.log import mask_dsn
from .db import (
    DatabaseUnavailableError,
    _inmemory_fallback_allowed,
    is_postgres_url,
)
from .db import to_sync_url as _to_sync_url

logger = logging.getLogger("orrery.memory")


def _extract_words_lower(text: str) -> set[str]:
    """Tokenize into lowercase words — mirrors ADK's in-memory search."""
    return {word.lower() for word in re.findall(r"\w+", text, re.UNICODE)}


# ── Default redaction patterns ───────────────────────────────────────

_DEFAULT_PATTERNS: list[re.Pattern[str]] = [
    # Key-value secrets: password=xxx, token: xxx, api_key=xxx, bearer xxx
    re.compile(
        r"(?i)(password|token|secret|api[_\-]?key|bearer|credential|auth)"
        r"\s*[:=]\s*\S+",
    ),
    # PEM private key blocks
    re.compile(
        r"-----BEGIN [A-Z ]+(?:PRIVATE )?KEY-----[\s\S]*?-----END [A-Z ]+(?:PRIVATE )?KEY-----",
    ),
]

_REDACTED = "[REDACTED]"


# ── Secure wrapper ───────────────────────────────────────────────────


class SecureMemoryService(BaseMemoryService):
    """Memory service wrapper that redacts secrets and caps storage.

    Redaction and trimming are applied on write, then the call is delegated
    to ``inner`` — an :class:`InMemoryMemoryService` by default, or a
    persistent :class:`DatabaseMemoryService` for durable recall.

    Args:
        inner: The backing memory service to delegate to. Defaults to a
            (non-persistent) :class:`InMemoryMemoryService`.
        max_entries_per_user: Maximum events kept per save. Oldest events
            are trimmed when the limit is exceeded.
        sensitive_patterns: Regex patterns for redaction. Defaults to a
            built-in set covering passwords, tokens, API keys, and PEM keys.
    """

    def __init__(
        self,
        *,
        inner: BaseMemoryService | None = None,
        max_entries_per_user: int = 500,
        sensitive_patterns: list[re.Pattern[str]] | None = None,
    ) -> None:
        self._inner = inner if inner is not None else InMemoryMemoryService()
        self._max_entries = max_entries_per_user
        self._patterns = sensitive_patterns if sensitive_patterns is not None else _DEFAULT_PATTERNS

    # ── Redaction helpers ────────────────────────────────────────────

    def _redact_text(self, text: str) -> str:
        """Apply all sensitive patterns to a text string."""
        for pattern in self._patterns:
            text = pattern.sub(_REDACTED, text)
        return text

    def _redact_content(self, content: types.Content) -> types.Content:
        """Return a deep copy of *content* with sensitive text redacted."""
        redacted = copy.deepcopy(content)
        if redacted.parts:
            for part in redacted.parts:
                if part.text:
                    part.text = self._redact_text(part.text)
        return redacted

    def _redact_events(self, events: Sequence[Event]) -> list[Event]:
        """Return copies of events with content redacted."""
        result: list[Event] = []
        for event in events:
            if event.content and event.content.parts:
                redacted_event = copy.deepcopy(event)
                redacted_event.content = self._redact_content(event.content)
                result.append(redacted_event)
            else:
                result.append(event)
        return result

    # ── Trim helpers ─────────────────────────────────────────────────

    def _trim_events(self, events: list[Event]) -> list[Event]:
        """Keep only the most recent events up to the per-user limit."""
        if len(events) <= self._max_entries:
            return events
        trimmed = len(events) - self._max_entries
        logger.debug(
            "Trimming %d oldest events to stay within %d limit", trimmed, self._max_entries
        )
        return events[-self._max_entries :]

    # ── BaseMemoryService interface ──────────────────────────────────

    async def add_session_to_memory(self, session: Session) -> None:
        """Redact, trim, then delegate to the inner service."""
        if not session.events:
            return

        # Build a shallow copy of the session with redacted + trimmed events
        redacted_events = self._redact_events(session.events)
        trimmed_events = self._trim_events(redacted_events)

        # Patch events on a copy to avoid mutating the live session
        patched = copy.copy(session)
        patched.events = trimmed_events

        await self._inner.add_session_to_memory(patched)
        logger.debug(
            "Saved session %s to memory (%d events, %d after trim)",
            session.id,
            len(session.events),
            len(trimmed_events),
        )

    async def add_events_to_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        events: Sequence[Event],
        session_id: str | None = None,
        custom_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Redact events then delegate to the inner service."""
        redacted = self._redact_events(events)
        trimmed = self._trim_events(redacted)
        await self._inner.add_events_to_memory(
            app_name=app_name,
            user_id=user_id,
            events=trimmed,
            session_id=session_id,
            custom_metadata=custom_metadata,
        )

    async def search_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        query: str,
    ) -> SearchMemoryResponse:
        """Delegate search to the inner service (already user-scoped)."""
        return await self._inner.search_memory(
            app_name=app_name,
            user_id=user_id,
            query=query,
        )


# ── Persistent backend ───────────────────────────────────────────────

_metadata = sa.MetaData()

# One row per memory-worthy event (i.e. events carrying content parts).
# Scoped by (app_name, user_id) to mirror ADK's per-user memory keying.
_memory_events = sa.Table(
    "orrery_memory_events",
    _metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("app_name", sa.String(255), nullable=False),
    sa.Column("user_id", sa.String(255), nullable=False),
    sa.Column("session_id", sa.String(255), nullable=False),
    sa.Column("event_id", sa.String(255), nullable=True),
    sa.Column("author", sa.String(255), nullable=True),
    sa.Column("ts", sa.Float, nullable=True),
    # Space-joined lowercase word tokens — enables keyword matching without
    # re-parsing content JSON for every non-matching row.
    sa.Column("search_text", sa.Text, nullable=False),
    sa.Column("content_json", sa.Text, nullable=False),
    sa.Index("ix_orrery_memory_scope", "app_name", "user_id"),
)


def _format_ts(ts: float | None) -> str | None:
    """Format a stored epoch timestamp as ISO 8601 (matches ADK)."""
    return datetime.fromtimestamp(ts).isoformat() if ts is not None else None


class DatabaseMemoryService(BaseMemoryService):
    """PostgreSQL-backed memory service for durable, cross-restart recall.

    Persists memory-worthy events to PostgreSQL so long-term memory is not lost
    on restart and is shared across processes/replicas. Search uses keyword
    matching identical to ADK's ``InMemoryMemoryService`` — not semantic
    search — but backed by durable storage rather than a process-local dict.

    The synchronous SQLAlchemy engine is driven from async methods via
    ``asyncio.to_thread`` (the codebase's standard pattern for blocking I/O),
    so no async database driver is required.

    Constructing the service opens a connection (to create the schema), so an
    unreachable database surfaces as a :class:`sqlalchemy.exc.SQLAlchemyError`
    here. Callers wanting a graceful fallback should use
    :func:`create_memory_service`, which catches that and reverts to in-memory.

    Args:
        db_url: PostgreSQL URL. The async ``+asyncpg`` driver is normalized to
            the sync ``+psycopg2`` driver used by the threadpool engine.
        echo: Emit SQL to the logger (debugging only).
        connect_timeout: Seconds to wait for the database connection before
            failing. Keeps startup from hanging when the database is unreachable.
    """

    def __init__(self, *, db_url: str, echo: bool = False, connect_timeout: int = 5) -> None:
        sync_url = _to_sync_url(db_url)
        # Fail fast instead of hanging when the server is unreachable
        # (psycopg2 honours connect_timeout, in seconds).
        self._engine = sa.create_engine(
            sync_url, echo=echo, future=True, connect_args={"connect_timeout": connect_timeout}
        )
        _metadata.create_all(self._engine)
        logger.info("Persistent memory store ready: %s", mask_dsn(sync_url))

    # ── Row helpers ──────────────────────────────────────────────────

    @staticmethod
    def _event_to_row(app_name: str, user_id: str, session_id: str, event: Event) -> dict[str, Any]:
        # Callers only pass events with content parts; assert to narrow the type.
        content = event.content
        assert content is not None and content.parts is not None
        text = " ".join(part.text for part in content.parts if part.text)
        return {
            "app_name": app_name,
            "user_id": user_id,
            "session_id": session_id,
            "event_id": event.id,
            "author": event.author,
            "ts": event.timestamp,
            "search_text": " ".join(sorted(_extract_words_lower(text))),
            "content_json": content.model_dump_json(),
        }

    # ── Sync DB operations (run inside a thread) ─────────────────────

    def _add_session_sync(self, session: Session) -> None:
        rows = [
            self._event_to_row(session.app_name, session.user_id, session.id, event)
            for event in session.events
            if event.content and event.content.parts
        ]
        with self._engine.begin() as conn:
            # Re-adding a session replaces its prior events (idempotent, matching
            # InMemoryMemoryService which overwrites the session's event list).
            conn.execute(
                sa.delete(_memory_events).where(
                    _memory_events.c.app_name == session.app_name,
                    _memory_events.c.user_id == session.user_id,
                    _memory_events.c.session_id == session.id,
                )
            )
            if rows:
                conn.execute(sa.insert(_memory_events), rows)

    def _add_events_sync(
        self, app_name: str, user_id: str, session_id: str, events: Sequence[Event]
    ) -> None:
        candidates = [e for e in events if e.content and e.content.parts]
        with self._engine.begin() as conn:
            existing = set(
                conn.execute(
                    sa.select(_memory_events.c.event_id).where(
                        _memory_events.c.app_name == app_name,
                        _memory_events.c.user_id == user_id,
                        _memory_events.c.session_id == session_id,
                    )
                ).scalars()
            )
            rows: list[dict[str, Any]] = []
            for event in candidates:
                if event.id in existing:  # incremental delta — skip duplicates
                    continue
                existing.add(event.id)
                rows.append(self._event_to_row(app_name, user_id, session_id, event))
            if rows:
                conn.execute(sa.insert(_memory_events), rows)

    def _search_sync(self, app_name: str, user_id: str, query: str) -> SearchMemoryResponse:
        words_in_query = _extract_words_lower(query)
        response = SearchMemoryResponse()
        if not words_in_query:
            return response
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.select(
                    _memory_events.c.content_json,
                    _memory_events.c.author,
                    _memory_events.c.ts,
                    _memory_events.c.search_text,
                )
                .where(
                    _memory_events.c.app_name == app_name,
                    _memory_events.c.user_id == user_id,
                    # Prefilter in SQL to avoid pulling a user's entire history
                    # into Python. ``search_text`` is space-joined word tokens,
                    # so an ILIKE substring match is a superset of the exact
                    # word match below (bound params — no injection risk); the
                    # Python check then drops any substring false-positives.
                    sa.or_(
                        *(
                            _memory_events.c.search_text.ilike(f"%{word}%")
                            for word in words_in_query
                        )
                    ),
                )
                .order_by(_memory_events.c.ts, _memory_events.c.id)
            )
            for content_json, author, ts, search_text in result:
                event_words = set(search_text.split())
                if not event_words:
                    continue
                if any(word in event_words for word in words_in_query):
                    response.memories.append(
                        MemoryEntry(
                            content=types.Content.model_validate_json(content_json),
                            author=author,
                            timestamp=_format_ts(ts),
                        )
                    )
        return response

    # ── BaseMemoryService interface ──────────────────────────────────

    async def add_session_to_memory(self, session: Session) -> None:
        if not session.events:
            return
        await asyncio.to_thread(self._add_session_sync, session)

    async def add_events_to_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        events: Sequence[Event],
        session_id: str | None = None,
        custom_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._add_events_sync,
            app_name,
            user_id,
            session_id or "__unknown_session_id__",
            events,
        )

    async def search_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        query: str,
    ) -> SearchMemoryResponse:
        return await asyncio.to_thread(self._search_sync, app_name, user_id, query)


# ── Factory ──────────────────────────────────────────────────────────


def create_memory_service(
    *,
    db_url: str | None = None,
    max_entries_per_user: int = 500,
    sensitive_patterns: list[re.Pattern[str]] | None = None,
) -> SecureMemoryService:
    """Build a redacting memory service: in-memory, or PostgreSQL when available.

    Resolution order for the backing store:

    1. Explicit ``db_url`` argument.
    2. ``DATABASE_URL`` environment variable (the same store used for sessions).
    3. Fallback to a process-local :class:`InMemoryMemoryService` (non-durable).

    Only PostgreSQL is supported for persistence. When a database URL is
    configured but cannot be honored (non-PostgreSQL, or PostgreSQL that is
    unreachable), this **fails fast** by raising
    :class:`~orrery_core.db.DatabaseUnavailableError` — mirroring the session
    store, so a pod does not come up "healthy" while hoarding recall in local
    memory. Set ``ORRERY_DB_ALLOW_INMEMORY_FALLBACK=1`` to opt into the
    in-memory fallback for local development.

    The result is always wrapped in a :class:`SecureMemoryService` so secret
    redaction and per-save trimming apply regardless of the backend.

    Args:
        db_url: Explicit PostgreSQL URL for the persistent backend.
        max_entries_per_user: Per-save event cap passed to the wrapper.
        sensitive_patterns: Custom redaction patterns for the wrapper.
    """
    resolved = db_url or os.getenv("DATABASE_URL")
    inner: BaseMemoryService
    if not resolved:
        logger.info("Using in-memory memory store — recall will be lost on restart")
        inner = InMemoryMemoryService()
    else:
        allow_fallback = _inmemory_fallback_allowed()
        if not is_postgres_url(resolved):
            reason = f"unsupported database URL {mask_dsn(resolved)} — only PostgreSQL is supported"
            if not allow_fallback:
                raise DatabaseUnavailableError(
                    f"PostgreSQL memory store unavailable ({reason}). Set "
                    "ORRERY_DB_ALLOW_INMEMORY_FALLBACK=1 to allow in-memory recall (local dev)."
                )
            logger.warning("%s — falling back to in-memory recall.", reason)
            inner = InMemoryMemoryService()
        else:
            try:
                inner = DatabaseMemoryService(db_url=resolved)
            except SQLAlchemyError as exc:
                if not allow_fallback:
                    raise DatabaseUnavailableError(
                        f"PostgreSQL memory store unavailable ({type(exc).__name__}: {exc}). "
                        "Refusing to start on non-durable in-memory recall while DATABASE_URL "
                        "is set. Fix the database connection, or set "
                        "ORRERY_DB_ALLOW_INMEMORY_FALLBACK=1 to allow the fallback (local dev)."
                    ) from exc
                logger.warning(
                    "PostgreSQL memory store unavailable (%s: %s) — falling back to "
                    "in-memory recall, which is lost on restart and not shared across "
                    "replicas. Verify DATABASE_URL points at a reachable PostgreSQL instance.",
                    type(exc).__name__,
                    exc,
                )
                inner = InMemoryMemoryService()
    return SecureMemoryService(
        inner=inner,
        max_entries_per_user=max_entries_per_user,
        sensitive_patterns=sensitive_patterns,
    )
