"""Database helpers: URL normalization, reachability probing, and a session
service factory with graceful fallback.

The platform supports exactly two session/memory stores: **in-memory** (when no
``DATABASE_URL`` is configured) and **PostgreSQL** (when it is). SQLite is not
supported.

ADK's ``DatabaseSessionService`` builds its async engine lazily and only touches
the database on the first request, so a misconfigured or unreachable database
would otherwise surface as a request-time crash rather than a startup error.
These helpers run a cheap *synchronous* pre-flight probe so the platform can
fall back to an in-memory session store (with a warning) instead of failing
hard when, say, Postgres isn't running yet.
"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from google.adk.sessions.base_session_service import BaseSessionService
from google.adk.sessions.database_session_service import DatabaseSessionService
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from .log import mask_dsn

logger = logging.getLogger("orrery.db")


def is_postgres_url(url: str) -> bool:
    """True if *url* points at PostgreSQL (the only supported database)."""
    return url.startswith("postgres")


def to_sync_url(url: str) -> str:
    """Normalize an async PostgreSQL URL to its sync-driver equivalent.

    The reachability probe uses a synchronous engine, so the async ``+asyncpg``
    driver is swapped for ``+psycopg2``. This lets callers pass the very same
    URL used for the async session store.
    """
    async_prefix, sync_prefix = "postgresql+asyncpg", "postgresql+psycopg2"
    if url.startswith(async_prefix):
        return sync_prefix + url[len(async_prefix) :]
    return url


def to_async_url(url: str) -> str:
    """Normalize a PostgreSQL URL to the async driver ADK's engine requires.

    ADK builds the session store with ``create_async_engine``, so
    ``postgresql://`` (or ``+psycopg2``) is rewritten to ``+asyncpg``.
    """
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql":
        return url
    # str(URL) masks the password as "***"; render the real value so the
    # connection actually works.
    return parsed.set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)


def database_reachable(db_url: str, *, connect_timeout: int = 5) -> bool:
    """Return ``True`` if a short-lived sync connection to *db_url* succeeds.

    The ``connect_timeout`` (seconds) makes an unreachable host fail fast
    instead of hanging startup.
    """
    engine = sa.create_engine(
        to_sync_url(db_url), connect_args={"connect_timeout": connect_timeout}
    )
    try:
        with engine.connect():
            return True
    except SQLAlchemyError as exc:
        logger.warning("Database unreachable (%s: %s)", type(exc).__name__, exc)
        return False
    finally:
        engine.dispose()


def create_session_service(db_url: str | None, *, connect_timeout: int = 5) -> BaseSessionService:
    """Build a session service: in-memory, or PostgreSQL when configured.

    Resolution:

    - No ``db_url`` → :class:`InMemorySessionService` (fine for local single
      process; a warning notes it is non-durable).
    - Non-PostgreSQL ``db_url`` → rejected (SQLite and friends are unsupported);
      falls back to in-memory with a warning.
    - Reachable PostgreSQL ``db_url`` → :class:`DatabaseSessionService`.
    - Configured but **unreachable** ``db_url`` → in-memory fallback plus a
      warning, rather than crashing startup.
    """
    if not db_url:
        logger.warning(
            "Using in-memory session store — sessions are lost on restart and "
            "cannot be shared across replicas."
        )
        return InMemorySessionService()

    if not is_postgres_url(db_url):
        logger.warning(
            "Unsupported database URL %s — only PostgreSQL is supported. "
            "Falling back to in-memory sessions.",
            mask_dsn(db_url),
        )
        return InMemorySessionService()

    fallback_reason: str | None = None
    if database_reachable(db_url, connect_timeout=connect_timeout):
        try:
            service = DatabaseSessionService(db_url=to_async_url(db_url))
        except Exception as exc:  # ADK wraps engine/driver errors as ValueError
            fallback_reason = f"session store init failed ({type(exc).__name__}: {exc})"
        else:
            logger.info("Using PostgreSQL session store: %s", mask_dsn(db_url))
            return service
    else:
        fallback_reason = "database unreachable"

    logger.warning(
        "PostgreSQL session store unavailable (%s) — falling back to in-memory sessions "
        "(lost on restart, not shared across replicas). Start PostgreSQL "
        "(e.g. `make infra-up`) to persist sessions.",
        fallback_reason,
    )
    return InMemorySessionService()
