"""Tests for the console's first-run diagnostics (AEP-019 Milestone 3)."""

from __future__ import annotations

import asyncio

import pytest

from orrery_core.serving.onboarding import (
    IntegrationProbe,
    _summarize,
    run_probe,
    run_probes,
)


def _probe(run, name="kafka", label="Kafka", hint="Set KAFKA_BOOTSTRAP_SERVERS."):
    return IntegrationProbe(name=name, label=label, hint=hint, run=run)


# ── _summarize ────────────────────────────────────────────────────────


def test_success_prefers_a_concrete_field():
    ok, detail = _summarize({"status": "success", "health": "healthy"})
    assert ok is True
    assert "healthy" in detail


def test_success_without_a_useful_field():
    assert _summarize({"status": "success"}) == (True, "Reachable.")


def test_error_carries_the_reason():
    """The reason is the whole point — an operator needs to know *which* host."""
    ok, detail = _summarize({"status": "error", "message": "Connection refused to broker-1:9092"})
    assert ok is False
    assert "broker-1:9092" in detail


def test_unexpected_result_type_is_not_a_pass():
    assert _summarize("not a dict")[0] is False
    assert _summarize(None)[0] is False


def test_long_error_is_truncated():
    ok, detail = _summarize({"status": "error", "message": "x" * 5000})
    assert ok is False
    assert len(detail) <= 300


# ── run_probe ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_healthy_probe():
    async def run():
        return {"status": "success", "health": "healthy"}

    result = await run_probe(_probe(run))
    assert result.ok is True
    assert result.hint == ""  # nothing to fix, so nothing to suggest
    assert result.duration_ms >= 0


@pytest.mark.asyncio
async def test_failed_probe_carries_the_hint():
    async def run():
        return {"status": "error", "message": "refused"}

    result = await run_probe(_probe(run))
    assert result.ok is False
    assert result.hint == "Set KAFKA_BOOTSTRAP_SERVERS."


@pytest.mark.asyncio
async def test_raising_probe_becomes_a_verdict():
    """A probe must never break the page it exists to render."""

    async def run():
        raise RuntimeError("no kubeconfig found")

    result = await run_probe(_probe(run))
    assert result.ok is False
    assert "no kubeconfig found" in result.detail
    assert result.hint


@pytest.mark.asyncio
async def test_hanging_probe_times_out(monkeypatch):
    """A first-run check that hangs is worse than one that fails — the user is
    sitting in front of it waiting to learn whether their config works."""
    monkeypatch.setattr("orrery_core.serving.onboarding.PROBE_TIMEOUT_SECONDS", 0.05)

    async def run():
        await asyncio.sleep(10)
        return {"status": "success"}

    result = await run_probe(_probe(run))
    assert result.ok is False
    assert "No response" in result.detail


# ── run_probes ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_probes_run_concurrently(monkeypatch):
    """One slow integration must not serialize the rest of the check."""

    async def slow():
        await asyncio.sleep(0.2)
        return {"status": "success"}

    probes = [_probe(slow, name=f"p{i}") for i in range(5)]

    start = asyncio.get_running_loop().time()
    results = await run_probes(probes)
    elapsed = asyncio.get_running_loop().time() - start

    assert len(results) == 5
    assert all(r.ok for r in results)
    assert elapsed < 0.5  # concurrent, not 5 x 0.2s


@pytest.mark.asyncio
async def test_no_probes_is_not_an_error():
    assert await run_probes([]) == []


@pytest.mark.asyncio
async def test_one_failure_does_not_hide_the_others():
    async def ok():
        return {"status": "success"}

    async def boom():
        raise RuntimeError("down")

    results = await run_probes([_probe(ok, name="a"), _probe(boom, name="b"), _probe(ok, name="c")])
    assert [r.ok for r in results] == [True, False, True]
