"""Tests for the Pub/Sub idempotency store (claim/release semantics).

The in-memory backend needs no server. The Postgres backend tests use the
``pg_url`` fixture and skip when no database is reachable.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest
from google_chat_bot.idempotency import (
    InMemoryIdempotencyStore,
    PostgresIdempotencyStore,
    create_idempotency_store,
    extract_event_id,
)
from sqlalchemy.exc import OperationalError

# ── extract_event_id ─────────────────────────────────────────────────


def test_extract_event_id_prefers_event_id():
    assert extract_event_id({"eventId": "abc123"}) == "abc123"
    assert extract_event_id({"event_id": "def456"}) == "def456"


def test_extract_event_id_is_stable_hash_when_absent():
    event = {
        "message": {
            "space": {"name": "spaces/AAA"},
            "thread": {"name": "spaces/AAA/threads/T1"},
            "createTime": "2026-07-04T10:00:00Z",
            "argumentText": "restart the api",
        }
    }
    a = extract_event_id(event)
    b = extract_event_id(dict(event))  # same content → same key
    assert a == b
    assert a.startswith("sha256:")


def test_extract_event_id_differs_for_different_events():
    base = {"message": {"space": {"name": "spaces/AAA"}, "argumentText": "scale to 3"}}
    other = {"message": {"space": {"name": "spaces/AAA"}, "argumentText": "scale to 9"}}
    assert extract_event_id(base) != extract_event_id(other)


# ── InMemoryIdempotencyStore ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_inmemory_first_claim_wins_second_is_duplicate():
    store = InMemoryIdempotencyStore()
    assert await store.claim("e1", ttl_seconds=60) is True
    assert await store.claim("e1", ttl_seconds=60) is False


@pytest.mark.asyncio
async def test_inmemory_release_allows_reclaim():
    store = InMemoryIdempotencyStore()
    assert await store.claim("e1", ttl_seconds=60) is True
    await store.release("e1")
    assert await store.claim("e1", ttl_seconds=60) is True


@pytest.mark.asyncio
async def test_inmemory_ttl_expiry_allows_reclaim():
    store = InMemoryIdempotencyStore()
    assert await store.claim("e1", ttl_seconds=0) is True
    # TTL of 0 means the claim is already expired on the next check.
    time.sleep(0.01)
    assert await store.claim("e1", ttl_seconds=60) is True


@pytest.mark.asyncio
async def test_inmemory_distinct_events_independent():
    store = InMemoryIdempotencyStore()
    assert await store.claim("e1", ttl_seconds=60) is True
    assert await store.claim("e2", ttl_seconds=60) is True


@pytest.mark.asyncio
async def test_inmemory_bounded_eviction():
    store = InMemoryIdempotencyStore(max_entries=3)
    for i in range(5):
        assert await store.claim(f"e{i}", ttl_seconds=60) is True
    # Oldest evicted → e0 is reclaimable; the newest still blocks.
    assert await store.claim("e0", ttl_seconds=60) is True
    assert await store.claim("e4", ttl_seconds=60) is False


@pytest.mark.asyncio
async def test_inmemory_concurrent_claims_exactly_one_winner():
    store = InMemoryIdempotencyStore()
    results = await asyncio.gather(*(store.claim("race", ttl_seconds=60) for _ in range(20)))
    assert sum(results) == 1  # exactly one True


# ── create_idempotency_store factory ─────────────────────────────────


def test_factory_memory_backend():
    assert isinstance(create_idempotency_store(backend="memory"), InMemoryIdempotencyStore)


def test_factory_rejects_unknown_backend():
    with pytest.raises(ValueError, match="Unknown idempotency backend"):
        create_idempotency_store(backend="redis")


def test_factory_postgres_requires_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="requires a database URL"):
        create_idempotency_store(backend="postgres")


# ── PostgresIdempotencyStore (skips without a live database) ──────────

_PG_URL = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL")


@pytest.fixture
def pg_store():
    if not _PG_URL:
        pytest.skip("No PostgreSQL TEST_DATABASE_URL/DATABASE_URL configured")
    # The constructor connects (schema create) and purge isolates each test
    # from prior rows — together they are also the reachability probe: a
    # configured-but-down database (e.g. the local compose postgres not
    # running while .env sets DATABASE_URL) must skip like an unconfigured
    # one, not error the suite.
    try:
        store = PostgresIdempotencyStore(db_url=_PG_URL)
        store.purge_expired()
    except OperationalError:
        pytest.skip(f"PostgreSQL configured but unreachable ({_PG_URL.split('@')[-1]})")
    return store


@pytest.mark.asyncio
async def test_postgres_first_claim_wins(pg_store):
    eid = f"pg-{time.time_ns()}"
    assert await pg_store.claim(eid, ttl_seconds=60) is True
    assert await pg_store.claim(eid, ttl_seconds=60) is False


@pytest.mark.asyncio
async def test_postgres_release_allows_reclaim(pg_store):
    eid = f"pg-{time.time_ns()}"
    assert await pg_store.claim(eid, ttl_seconds=60) is True
    await pg_store.release(eid)
    assert await pg_store.claim(eid, ttl_seconds=60) is True


@pytest.mark.asyncio
async def test_postgres_expired_claim_reclaimable(pg_store):
    eid = f"pg-{time.time_ns()}"
    assert await pg_store.claim(eid, ttl_seconds=0) is True  # already expired
    assert await pg_store.claim(eid, ttl_seconds=60) is True  # reclaimed


@pytest.mark.asyncio
async def test_postgres_concurrent_claims_exactly_one_winner(pg_store):
    eid = f"pg-race-{time.time_ns()}"
    results = await asyncio.gather(*(pg_store.claim(eid, ttl_seconds=60) for _ in range(10)))
    assert sum(results) == 1
