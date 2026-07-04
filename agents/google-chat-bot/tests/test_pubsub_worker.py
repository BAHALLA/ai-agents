"""Tests for the Pub/Sub subscriber worker."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from google_chat_bot import pubsub_worker
from google_chat_bot.idempotency import InMemoryIdempotencyStore


class FakeMessage:
    """Minimal stand-in for ``google.cloud.pubsub_v1.subscriber.message.Message``."""

    def __init__(self, data: bytes, message_id: str = "msg-1"):
        self.data = data
        self.message_id = message_id
        self.acked = False
        self.nacked = False

    def ack(self) -> None:
        self.acked = True

    def nack(self) -> None:
        self.nacked = True


@pytest.fixture
def handler():
    h = MagicMock()
    h.handle_event = AsyncMock(return_value={"text": "ok"})
    return h


@pytest.mark.asyncio
async def test_callback_dispatches_event_and_acks(handler):
    """Happy path: well-formed event runs the handler and acks the message."""
    loop = asyncio.get_running_loop()
    callback = pubsub_worker.make_callback(handler, loop, timeout_seconds=5)

    event = {"type": "MESSAGE", "message": {"argumentText": "hi"}}
    msg = FakeMessage(json.dumps(event).encode("utf-8"))

    # callback is sync and would normally run in the SubscriberClient's
    # thread pool — emulate that by offloading to a worker thread so the
    # event loop is free to service ``run_coroutine_threadsafe``.
    await asyncio.to_thread(callback, msg)

    assert msg.acked is True
    assert msg.nacked is False
    handler.handle_event.assert_awaited_once_with(event)


@pytest.mark.asyncio
async def test_callback_acks_malformed_json(handler):
    """Non-JSON payloads are unrecoverable and must not be redelivered."""
    loop = asyncio.get_running_loop()
    callback = pubsub_worker.make_callback(handler, loop, timeout_seconds=5)

    msg = FakeMessage(b"not-json", message_id="bad-1")
    await asyncio.to_thread(callback, msg)

    assert msg.acked is True
    handler.handle_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_acks_non_object_payload(handler):
    """JSON arrays / scalars are not Chat events — drop them."""
    loop = asyncio.get_running_loop()
    callback = pubsub_worker.make_callback(handler, loop, timeout_seconds=5)

    msg = FakeMessage(json.dumps(["not", "an", "object"]).encode("utf-8"))
    await asyncio.to_thread(callback, msg)

    assert msg.acked is True
    handler.handle_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_nacks_when_handler_raises():
    """Handler errors trigger nack so Pub/Sub can redeliver."""
    handler = MagicMock()
    handler.handle_event = AsyncMock(side_effect=RuntimeError("boom"))

    loop = asyncio.get_running_loop()
    callback = pubsub_worker.make_callback(handler, loop, timeout_seconds=5)

    msg = FakeMessage(json.dumps({"type": "MESSAGE"}).encode("utf-8"))
    await asyncio.to_thread(callback, msg)

    assert msg.nacked is True
    assert msg.acked is False


# ── Idempotency (AEP-018) ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_event_acked_without_reinvoking_handler(handler):
    """A redelivered event (same eventId) is acked and dropped, not re-run."""
    loop = asyncio.get_running_loop()
    store = InMemoryIdempotencyStore()
    callback = pubsub_worker.make_callback(handler, loop, timeout_seconds=5, store=store)

    event = {"eventId": "evt-dup", "type": "MESSAGE", "message": {"argumentText": "restart api"}}
    data = json.dumps(event).encode("utf-8")

    first = FakeMessage(data, message_id="m1")
    second = FakeMessage(data, message_id="m2")  # same logical event, redelivered
    await asyncio.to_thread(callback, first)
    await asyncio.to_thread(callback, second)

    # Handler ran exactly once; both deliveries were acked (neither nacked).
    handler.handle_event.assert_awaited_once_with(event)
    assert first.acked is True and first.nacked is False
    assert second.acked is True and second.nacked is False


@pytest.mark.asyncio
async def test_distinct_events_both_processed(handler):
    """Different events are not confused for duplicates."""
    loop = asyncio.get_running_loop()
    store = InMemoryIdempotencyStore()
    callback = pubsub_worker.make_callback(handler, loop, timeout_seconds=5, store=store)

    a = FakeMessage(json.dumps({"eventId": "a"}).encode("utf-8"), message_id="ma")
    b = FakeMessage(json.dumps({"eventId": "b"}).encode("utf-8"), message_id="mb")
    await asyncio.to_thread(callback, a)
    await asyncio.to_thread(callback, b)

    assert handler.handle_event.await_count == 2
    assert a.acked and b.acked


@pytest.mark.asyncio
async def test_failed_handler_releases_claim_so_redelivery_retries():
    """A handler failure releases the claim; the redelivered event is retried."""
    handler = MagicMock()
    # First delivery raises; the redelivery succeeds.
    handler.handle_event = AsyncMock(side_effect=[RuntimeError("boom"), {"text": "ok"}])

    loop = asyncio.get_running_loop()
    store = InMemoryIdempotencyStore()
    callback = pubsub_worker.make_callback(handler, loop, timeout_seconds=5, store=store)

    data = json.dumps({"eventId": "evt-retry", "type": "MESSAGE"}).encode("utf-8")
    first = FakeMessage(data, message_id="m1")
    redelivery = FakeMessage(data, message_id="m2")

    await asyncio.to_thread(callback, first)
    assert first.nacked is True and first.acked is False  # released + nacked

    await asyncio.to_thread(callback, redelivery)
    assert redelivery.acked is True  # claim was released, so it re-ran and succeeded
    assert handler.handle_event.await_count == 2


@pytest.mark.asyncio
async def test_store_claim_error_nacks(handler):
    """If the idempotency store errors, nack rather than risk a double-run."""
    loop = asyncio.get_running_loop()

    class BrokenStore:
        async def claim(self, event_id, *, ttl_seconds):
            raise RuntimeError("store down")

        async def release(self, event_id):
            pass

    callback = pubsub_worker.make_callback(handler, loop, timeout_seconds=5, store=BrokenStore())
    msg = FakeMessage(json.dumps({"eventId": "x"}).encode("utf-8"))
    await asyncio.to_thread(callback, msg)

    assert msg.nacked is True
    handler.handle_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_nacks_on_timeout():
    """A handler that exceeds the timeout is cancelled and the message is nacked."""
    handler = MagicMock()

    async def slow_handler(_event):
        await asyncio.sleep(10)
        return {"text": "never"}

    handler.handle_event = slow_handler

    loop = asyncio.get_running_loop()
    callback = pubsub_worker.make_callback(handler, loop, timeout_seconds=0.1)

    msg = FakeMessage(json.dumps({"type": "MESSAGE"}).encode("utf-8"))
    await asyncio.to_thread(callback, msg)

    assert msg.nacked is True
    assert msg.acked is False


def test_resolve_subscription_path_accepts_full_path(monkeypatch):
    monkeypatch.setattr(
        pubsub_worker.config,
        "google_chat_pubsub_subscription",
        "projects/my-proj/subscriptions/my-sub",
    )
    client = MagicMock()
    assert (
        pubsub_worker.resolve_subscription_path(client) == "projects/my-proj/subscriptions/my-sub"
    )
    client.subscription_path.assert_not_called()


def test_resolve_subscription_path_qualifies_short_id(monkeypatch):
    monkeypatch.setattr(pubsub_worker.config, "google_chat_pubsub_subscription", "my-sub")
    monkeypatch.setattr(pubsub_worker.config, "google_chat_pubsub_project", "my-proj")
    client = MagicMock()
    client.subscription_path.return_value = "projects/my-proj/subscriptions/my-sub"

    result = pubsub_worker.resolve_subscription_path(client)
    assert result == "projects/my-proj/subscriptions/my-sub"
    client.subscription_path.assert_called_once_with("my-proj", "my-sub")


def test_resolve_subscription_path_falls_back_to_google_cloud_project(monkeypatch):
    monkeypatch.setattr(pubsub_worker.config, "google_chat_pubsub_subscription", "my-sub")
    monkeypatch.setattr(pubsub_worker.config, "google_chat_pubsub_project", None)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "fallback-proj")
    client = MagicMock()
    client.subscription_path.return_value = "projects/fallback-proj/subscriptions/my-sub"

    pubsub_worker.resolve_subscription_path(client)
    client.subscription_path.assert_called_once_with("fallback-proj", "my-sub")


def test_resolve_subscription_path_requires_subscription(monkeypatch):
    monkeypatch.setattr(pubsub_worker.config, "google_chat_pubsub_subscription", None)
    with pytest.raises(RuntimeError, match="GOOGLE_CHAT_PUBSUB_SUBSCRIPTION"):
        pubsub_worker.resolve_subscription_path(MagicMock())


def test_resolve_subscription_path_requires_project(monkeypatch):
    monkeypatch.setattr(pubsub_worker.config, "google_chat_pubsub_subscription", "my-sub")
    monkeypatch.setattr(pubsub_worker.config, "google_chat_pubsub_project", None)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    with pytest.raises(RuntimeError, match="GOOGLE_CHAT_PUBSUB_PROJECT"):
        pubsub_worker.resolve_subscription_path(MagicMock())


# ── Health server ───────────────────────────────────────────────────────


def test_health_server_readiness_fails_without_future():
    """readiness check reports false until the streaming pull future is registered."""
    server = pubsub_worker._build_health_server({})
    ok, details = server._run_checks()
    assert not ok
    assert details == {"pubsub_subscriber": False}


def test_health_server_readiness_ok_with_live_future():
    future = MagicMock()
    future.done.return_value = False
    server = pubsub_worker._build_health_server({"future": future})
    ok, details = server._run_checks()
    assert ok
    assert details == {"pubsub_subscriber": True}


def test_health_server_readiness_fails_when_future_done():
    """If the streaming pull dies, the readiness check flips to false."""
    future = MagicMock()
    future.done.return_value = True
    server = pubsub_worker._build_health_server({"future": future})
    ok, details = server._run_checks()
    assert not ok
    assert details == {"pubsub_subscriber": False}
