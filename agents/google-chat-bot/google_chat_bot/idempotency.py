"""Event-level idempotency for the Pub/Sub worker.

Google Cloud Pub/Sub delivers **at least once**: the same message can be
redelivered when an ack is lost to a network blip, a pod OOMs mid-callback, or a
handler timeout nacks after side effects have already landed. Because the Chat
handler can invoke ``@destructive`` tools (restart / scale / rollback a
deployment, increase Kafka partitions, silence Alertmanager…), a redelivered
event would **double-act** on those tools. This module short-circuits duplicates.

The contract is a two-call claim/release protocol:

- :meth:`IdempotencyStore.claim` — atomically record an ``event_id`` and return
  ``True`` only for the *first* caller within the TTL window. A ``False`` return
  means the event was already processed (or is in flight elsewhere) and the
  caller should ack-and-drop without re-executing.
- :meth:`IdempotencyStore.release` — undo a claim so a **failed** handler can be
  retried on redelivery. Success keeps the claim (until TTL) so genuine
  duplicates stay blocked; failure releases it so the work is not lost.

Two backends:

- :class:`InMemoryIdempotencyStore` — bounded, single-process. The correct
  choice only for a **single-replica** worker (dev, or a pinned 1-replica prod).
- :class:`PostgresIdempotencyStore` — shared across replicas via
  ``INSERT … ON CONFLICT DO NOTHING``. Required for ``replicaCount > 1``. Reuses
  the Postgres the platform already runs for sessions/memory — no extra infra.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict
from typing import Any, Protocol, runtime_checkable

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from orrery_core.persistence.db import to_sync_url as _to_sync_url

logger = logging.getLogger("google_chat_bot.idempotency")


# ── Dedup key extraction ─────────────────────────────────────────────


def extract_event_id(event: dict[str, Any]) -> str:
    """Derive a stable dedup key for a Google Chat event.

    Prefers a Chat-provided ``eventId`` (present on most event shapes). When
    absent, falls back to a SHA-256 of the space, thread, creation time, and
    message text — stable across redeliveries of the *same* logical event but
    distinct for genuinely different events.
    """
    for key in ("eventId", "event_id"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value

    # Fall back to a content hash. Reach into the common Chat/Add-ons shapes for
    # the fields that identify a logical event; missing pieces just contribute
    # empty strings, which is fine — the tuple as a whole is still distinctive.
    chat = event.get("chat") or {}
    msg_payload = chat.get("messagePayload") or {}
    message = event.get("message") or msg_payload.get("message") or {}
    space = message.get("space") or chat.get("space") or event.get("space") or {}
    thread = message.get("thread") or {}

    parts = [
        str(space.get("name", "")) if isinstance(space, dict) else "",
        str(thread.get("name", "")) if isinstance(thread, dict) else "",
        str(message.get("createTime", "")),
        str(message.get("argumentText") or message.get("text") or ""),
    ]
    digest = hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


# ── Store protocol ───────────────────────────────────────────────────


@runtime_checkable
class IdempotencyStore(Protocol):
    """Atomic first-seen claim with a failure-path release."""

    async def claim(self, event_id: str, *, ttl_seconds: int) -> bool:
        """Return ``True`` iff *event_id* is claimed for the first time.

        A ``False`` return means the event is a duplicate (already processed or
        in flight) and must not be re-executed.
        """
        ...

    async def release(self, event_id: str) -> None:
        """Drop a prior claim so a failed handler can retry on redelivery."""
        ...


# ── In-memory backend ────────────────────────────────────────────────


class InMemoryIdempotencyStore:
    """Process-local claim store — correct only for a single worker replica.

    Entries expire after their TTL; the map is capped at ``max_entries`` with
    oldest-first eviction so a long-running worker cannot grow without bound.
    """

    def __init__(self, *, max_entries: int = 10_000) -> None:
        self._max_entries = max_entries
        # event_id -> expiry epoch seconds. OrderedDict gives FIFO eviction.
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._lock = asyncio.Lock()

    async def claim(self, event_id: str, *, ttl_seconds: int) -> bool:
        now = time.monotonic()
        async with self._lock:
            self._evict_expired(now)
            expiry = self._seen.get(event_id)
            if expiry is not None and expiry > now:
                return False  # live claim — duplicate
            # First-seen, or a prior claim already expired: (re)claim it.
            self._seen[event_id] = now + ttl_seconds
            self._seen.move_to_end(event_id)
            while len(self._seen) > self._max_entries:
                self._seen.popitem(last=False)
            return True

    async def release(self, event_id: str) -> None:
        async with self._lock:
            self._seen.pop(event_id, None)

    def _evict_expired(self, now: float) -> None:
        # OrderedDict is not strictly ordered by expiry (TTL is uniform, so it
        # effectively is), but iterate defensively and stop at the first live one.
        for key in list(self._seen.keys()):
            if self._seen[key] <= now:
                del self._seen[key]
            else:
                break


# ── Postgres backend ─────────────────────────────────────────────────

_metadata = sa.MetaData()

_idempotency_events = sa.Table(
    "orrery_pubsub_idempotency",
    _metadata,
    sa.Column("event_id", sa.String(512), primary_key=True),
    sa.Column("claimed_at", sa.Float, nullable=False),
    sa.Column("expires_at", sa.Float, nullable=False, index=True),
)


class PostgresIdempotencyStore:
    """Cross-replica claim store backed by PostgreSQL.

    ``claim`` is a single ``INSERT … ON CONFLICT DO NOTHING`` — atomic across
    replicas, so exactly one worker wins a race for the same ``event_id``. An
    existing-but-expired claim is treated as reclaimable (the insert would
    conflict, so we then check/refresh the row under the same guarantee).

    The synchronous SQLAlchemy engine is driven from async methods via
    ``asyncio.to_thread`` — the same pattern the memory backend uses — so no
    async driver is required.
    """

    def __init__(self, *, db_url: str, connect_timeout: int = 5) -> None:
        self._engine = sa.create_engine(
            _to_sync_url(db_url),
            future=True,
            connect_args={"connect_timeout": connect_timeout},
        )
        _metadata.create_all(self._engine)
        logger.info("Pub/Sub idempotency store ready (PostgreSQL)")

    async def claim(self, event_id: str, *, ttl_seconds: int) -> bool:
        return await asyncio.to_thread(self._claim_sync, event_id, ttl_seconds)

    async def release(self, event_id: str) -> None:
        await asyncio.to_thread(self._release_sync, event_id)

    def _claim_sync(self, event_id: str, ttl_seconds: int) -> bool:
        now = time.time()
        expires_at = now + ttl_seconds
        with self._engine.begin() as conn:
            # Fast path: fresh insert wins the claim.
            inserted = conn.execute(
                pg_insert(_idempotency_events)
                .values(event_id=event_id, claimed_at=now, expires_at=expires_at)
                .on_conflict_do_nothing(index_elements=["event_id"])
            )
            if inserted.rowcount and inserted.rowcount > 0:
                return True
            # Conflict: a row exists. Reclaim it only if it has expired. The
            # UPDATE is conditional on expiry, so two racing workers cannot both
            # win — exactly one UPDATE touches the row.
            reclaimed = conn.execute(
                sa.update(_idempotency_events)
                .where(
                    _idempotency_events.c.event_id == event_id,
                    _idempotency_events.c.expires_at <= now,
                )
                .values(claimed_at=now, expires_at=expires_at)
            )
            return bool(reclaimed.rowcount and reclaimed.rowcount > 0)

    def _release_sync(self, event_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.delete(_idempotency_events).where(_idempotency_events.c.event_id == event_id)
            )

    def purge_expired(self) -> int:
        """Delete expired rows; returns the count. For an optional cleanup job."""
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.delete(_idempotency_events).where(
                    _idempotency_events.c.expires_at <= time.time()
                )
            )
        return int(result.rowcount or 0)


# ── Factory ──────────────────────────────────────────────────────────


def create_idempotency_store(
    *,
    backend: str,
    db_url: str | None = None,
) -> IdempotencyStore:
    """Build the configured idempotency store.

    Args:
        backend: ``"memory"`` (single-replica) or ``"postgres"`` (multi-replica).
        db_url: PostgreSQL URL for the ``postgres`` backend. Falls back to the
            shared ``DATABASE_URL`` when omitted.

    Raises:
        ValueError: unknown backend, or ``postgres`` selected with no DB URL.
    """
    backend = (backend or "memory").strip().lower()
    if backend == "memory":
        return InMemoryIdempotencyStore()
    if backend == "postgres":
        import os

        resolved = db_url or os.getenv("DATABASE_URL")
        if not resolved:
            raise ValueError(
                "Pub/Sub idempotency backend 'postgres' requires a database URL "
                "(set DATABASE_URL). Use backend 'memory' only for a single replica."
            )
        return PostgresIdempotencyStore(db_url=resolved)
    raise ValueError(f"Unknown idempotency backend {backend!r} (expected 'memory' or 'postgres')")
