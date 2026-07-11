"""Tests for the confirmation store backends (memory + PostgreSQL).

The Postgres tests mirror the idempotency-store pattern: they use the
DATABASE_URL from the environment and skip — never error — when the
database is unconfigured or unreachable.
"""

from __future__ import annotations

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from google_chat_bot.confirmation import (
    _APPROVAL_VALIDITY,
    _CONFIRMATION_TTL,
    ConfirmationStore,
    PendingConfirmation,
    PostgresConfirmationStore,
    create_confirmation_store,
)
from sqlalchemy.exc import OperationalError


def _pending(**overrides) -> PendingConfirmation:
    base = PendingConfirmation(
        action_id=uuid.uuid4().hex[:12],
        tool_name="delete_topic",
        user_id="alice@example.com",
        session_id="gchat:threads/T1",
        space_name="spaces/S1",
        thread_name="threads/T1",
        level="destructive",
        args={"topic": "events"},
        args_hash="feedface",
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


# ── Factory ──────────────────────────────────────────────────────────


def test_factory_memory_backend():
    assert isinstance(create_confirmation_store(backend="memory"), ConfirmationStore)


def test_factory_rejects_unknown_backend():
    with pytest.raises(ValueError, match="Unknown confirmation backend"):
        create_confirmation_store(backend="redis")


def test_factory_postgres_requires_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="requires a database URL"):
        create_confirmation_store(backend="postgres")


# ── Memory backend: mark_approved (new store-mediated write) ─────────


def test_memory_mark_approved_sets_fields():
    store = ConfirmationStore()
    p = _pending()
    store.add(p)

    marked = store.mark_approved(p.action_id)
    assert marked is not None and marked.approved is True
    assert marked.approved_at is not None
    fetched = store.get(p.action_id)
    assert fetched is not None and fetched.approved is True


def test_memory_mark_approved_missing_returns_none():
    assert ConfirmationStore().mark_approved("nope") is None


# ── Postgres backend (skips without a reachable database) ────────────

_PG_URL = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL")


@pytest.fixture
def pg_store():
    if not _PG_URL:
        pytest.skip("No PostgreSQL TEST_DATABASE_URL/DATABASE_URL configured")
    # Constructor (schema create) + purge double as the reachability probe:
    # configured-but-down must skip like unconfigured, not error the suite.
    try:
        store = PostgresConfirmationStore(db_url=_PG_URL)
        store.purge_expired()
    except OperationalError:
        pytest.skip(f"PostgreSQL configured but unreachable ({_PG_URL.split('@')[-1]})")
    return store


def test_pg_add_get_pop_roundtrip(pg_store):
    p = _pending()
    pg_store.add(p)

    fetched = pg_store.get(p.action_id)
    assert fetched is not None
    assert fetched.tool_name == "delete_topic"
    assert fetched.args == {"topic": "events"}
    assert fetched.user_id == "alice@example.com"

    popped = pg_store.pop(p.action_id)
    assert popped is not None and popped.action_id == p.action_id
    assert pg_store.get(p.action_id) is None


def test_pg_mark_approved_persists(pg_store):
    p = _pending()
    pg_store.add(p)
    marked = pg_store.mark_approved(p.action_id)
    assert marked is not None and marked.approved is True
    # A *fresh read* sees the approval — the property in-place mutation lacked.
    fetched = pg_store.get(p.action_id)
    assert fetched is not None and fetched.approved is True
    pg_store.pop(p.action_id)


def test_pg_latest_for_thread_orders_and_matches_space(pg_store):
    thread = f"threads/{uuid.uuid4().hex[:8]}"
    older = _pending(thread_name=thread, created_at=time.time() - 10)
    newer = _pending(thread_name=thread)
    pg_store.add(older)
    pg_store.add(newer)

    latest = pg_store.latest_for_thread(thread)
    assert latest is not None and latest.action_id == newer.action_id
    # Space key matches too (quick-reply events may only carry the space).
    assert pg_store.latest_for_thread("spaces/S1") is not None

    pg_store.pop(older.action_id)
    pg_store.pop(newer.action_id)


def test_pg_expired_pending_is_invisible(pg_store):
    thread = f"threads/{uuid.uuid4().hex[:8]}"
    stale = _pending(thread_name=thread, created_at=time.time() - _CONFIRMATION_TTL - 5)
    pg_store.add(stale)
    assert pg_store.latest_for_thread(thread) is None


def test_pg_mark_latest_then_consume_is_one_shot(pg_store):
    thread = f"threads/{uuid.uuid4().hex[:8]}"
    p = _pending(thread_name=thread)
    pg_store.add(p)

    marked = pg_store.mark_latest_approved_for_thread(thread)
    assert marked is not None and marked.approved is True

    consumed = pg_store.consume_approved(thread, "delete_topic", "feedface")
    assert consumed is not None and consumed.action_id == p.action_id
    # One-shot: a second consume finds nothing.
    assert pg_store.consume_approved(thread, "delete_topic", "feedface") is None


def test_pg_consume_ignores_stale_approvals(pg_store):
    thread = f"threads/{uuid.uuid4().hex[:8]}"
    p = _pending(
        thread_name=thread,
        approved=True,
        approved_at=time.time() - _APPROVAL_VALIDITY - 5,
    )
    pg_store.add(p)
    assert pg_store.consume_approved(thread, "delete_topic", "feedface") is None
    pg_store.pop(p.action_id)


def test_pg_consume_requires_matching_args_hash(pg_store):
    thread = f"threads/{uuid.uuid4().hex[:8]}"
    p = _pending(thread_name=thread, approved=True, approved_at=time.time())
    pg_store.add(p)
    assert pg_store.consume_approved(thread, "delete_topic", "different") is None
    assert pg_store.consume_approved(thread, "delete_topic", "feedface") is not None


def test_pg_pop_latest_for_thread_removes(pg_store):
    thread = f"threads/{uuid.uuid4().hex[:8]}"
    p = _pending(thread_name=thread)
    pg_store.add(p)
    popped = pg_store.pop_latest_for_thread(thread)
    assert popped is not None and popped.action_id == p.action_id
    assert pg_store.latest_for_thread(thread) is None


def test_pg_concurrent_consume_exactly_one_winner(pg_store):
    """The cross-replica guarantee: racing consumers get exactly one approval."""
    thread = f"threads/{uuid.uuid4().hex[:8]}"
    p = _pending(thread_name=thread, approved=True, approved_at=time.time())
    pg_store.add(p)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: pg_store.consume_approved(thread, "delete_topic", "feedface"),
                range(8),
            )
        )
    assert sum(1 for r in results if r is not None) == 1
