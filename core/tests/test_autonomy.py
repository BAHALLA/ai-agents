"""Tests for orrery_core.plugins.autonomy_plugin."""

from __future__ import annotations

from typing import Any

import pytest

from orrery_core.plugins.autonomy_plugin import AUTONOMY_LEVEL_STATE_KEY, AutonomyPlugin
from orrery_core.security.guardrails import confirm, destructive

# ── Fixtures ───────────────────────────────────────────────────────────


class _Confirmation:
    def __init__(self, confirmed: bool):
        self.confirmed = confirmed


class ConfirmingToolContext:
    """Minimal ToolContext supporting ADK's native confirmation API."""

    def __init__(self, confirmed: bool | None = None):
        self.state: dict = {}
        self.tool_confirmation = _Confirmation(confirmed) if confirmed is not None else None
        self.requested_hint: str | None = None

    def request_confirmation(self, *, hint: str) -> None:
        self.requested_hint = hint


def _confirming_ctx(confirmed: bool | None = None) -> Any:
    """Any-typed so it can stand in for ADK's ToolContext in plugin calls."""
    return ConfirmingToolContext(confirmed)


def _read_tool(fake_tool):
    def list_things():
        pass

    return fake_tool(name="list_things", func=list_things)


def _mutating_tool(fake_tool):
    @confirm("scales the deployment")
    def scale_deployment():
        pass

    return fake_tool(name="scale_deployment", func=scale_deployment)


def _destructive_tool(fake_tool):
    @destructive("deletes the topic")
    def delete_topic():
        pass

    return fake_tool(name="delete_topic", func=delete_topic)


# ── L2 (read-only, fail-closed) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_l2_allows_read_blocks_mutation_and_destructive(fake_tool, fake_ctx):
    plugin = AutonomyPlugin(level="L2")
    ctx = fake_ctx()

    assert (
        await plugin.before_tool_callback(
            tool=_read_tool(fake_tool), tool_args={}, tool_context=ctx
        )
        is None
    )
    for tool in (_mutating_tool(fake_tool), _destructive_tool(fake_tool)):
        deny = await plugin.before_tool_callback(tool=tool, tool_args={}, tool_context=ctx)
        assert deny is not None
        assert deny["status"] == "BLOCKED"
        assert deny["autonomy_level"] == "L2"


@pytest.mark.asyncio
async def test_l2_whitelist_override(fake_tool, fake_ctx):
    plugin = AutonomyPlugin(level="L2", l2_whitelist=["delete_topic"])
    result = await plugin.before_tool_callback(
        tool=_destructive_tool(fake_tool), tool_args={}, tool_context=fake_ctx()
    )
    assert result is None


# ── L3 (mutating, destructive blocked) ─────────────────────────────────


@pytest.mark.asyncio
async def test_l3_allows_mutation_blocks_destructive(fake_tool, fake_ctx):
    plugin = AutonomyPlugin(level="L3")
    ctx = fake_ctx()

    assert (
        await plugin.before_tool_callback(
            tool=_mutating_tool(fake_tool), tool_args={}, tool_context=ctx
        )
        is None
    )
    deny = await plugin.before_tool_callback(
        tool=_destructive_tool(fake_tool), tool_args={}, tool_context=ctx
    )
    assert deny is not None
    assert deny["status"] == "BLOCKED"


@pytest.mark.asyncio
async def test_l3_blacklist_blocks_named_tool(fake_tool, fake_ctx):
    plugin = AutonomyPlugin(level="L3", l3_blacklist=["scale_deployment"])
    deny = await plugin.before_tool_callback(
        tool=_mutating_tool(fake_tool), tool_args={}, tool_context=fake_ctx()
    )
    assert deny is not None
    assert deny["status"] == "BLOCKED"


# ── L4 (destructive requires ADK-native HITL confirmation) ─────────────


@pytest.mark.asyncio
async def test_l4_requests_confirmation_then_honors_answer(fake_tool, fake_ctx):
    plugin = AutonomyPlugin(level="L4")
    tool = _destructive_tool(fake_tool)

    # No confirmation yet → request one and pause.
    ctx = _confirming_ctx()
    deny = await plugin.before_tool_callback(tool=tool, tool_args={"t": "x"}, tool_context=ctx)
    assert ctx.requested_hint is not None
    assert deny is not None
    assert deny["status"] == "BLOCKED"

    # User approved → proceed.
    approved = _confirming_ctx(confirmed=True)
    assert await plugin.before_tool_callback(tool=tool, tool_args={}, tool_context=approved) is None

    # User rejected → blocked.
    rejected = _confirming_ctx(confirmed=False)
    deny = await plugin.before_tool_callback(tool=tool, tool_args={}, tool_context=rejected)
    assert deny is not None
    assert deny["status"] == "BLOCKED"


@pytest.mark.asyncio
async def test_l4_mutating_tool_needs_no_confirmation(fake_tool, fake_ctx):
    plugin = AutonomyPlugin(level="L4")
    result = await plugin.before_tool_callback(
        tool=_mutating_tool(fake_tool), tool_args={}, tool_context=fake_ctx()
    )
    assert result is None


# ── Level resolution ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_state_overrides_configured_level(fake_tool, fake_ctx):
    # Configured L2, state bumps to L3 → mutation allowed, destructive blocked.
    plugin = AutonomyPlugin(level="L2")
    ctx = fake_ctx(state={AUTONOMY_LEVEL_STATE_KEY: "L3"})

    assert (
        await plugin.before_tool_callback(
            tool=_mutating_tool(fake_tool), tool_args={}, tool_context=ctx
        )
        is None
    )
    deny = await plugin.before_tool_callback(
        tool=_destructive_tool(fake_tool), tool_args={}, tool_context=ctx
    )
    assert deny is not None
    assert deny["status"] == "BLOCKED"


def test_invalid_level_normalises_to_l2():
    assert AutonomyPlugin(level="banana")._level == "L2"
    assert AutonomyPlugin(level="l3")._level == "L3"
