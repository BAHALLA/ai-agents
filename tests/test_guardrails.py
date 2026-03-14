"""Tests for ai_agents_core.guardrails."""

from ai_agents_core.guardrails import (
    destructive,
    dry_run,
    get_destructive_reason,
    is_destructive,
    require_confirmation,
)
from conftest import FakeTool, FakeToolContext


# ── @destructive decorator ─────────────────────────────────────────────


def test_destructive_marks_function():
    @destructive("deletes everything")
    def my_tool():
        pass

    assert is_destructive(my_tool) is True
    assert get_destructive_reason(my_tool) == "deletes everything"


def test_unmarked_function_is_not_destructive():
    def safe_tool():
        pass

    assert is_destructive(safe_tool) is False
    assert get_destructive_reason(safe_tool) == ""


def test_destructive_with_empty_reason():
    @destructive()
    def my_tool():
        pass

    assert is_destructive(my_tool) is True
    assert get_destructive_reason(my_tool) == ""


def test_is_destructive_checks_func_attr():
    """ADK wraps functions in BaseTool objects with a .func attribute."""
    @destructive("reason")
    def my_func():
        pass

    # Simulate ADK wrapping: tool.func = my_func
    tool = FakeTool(name="my_func", func=my_func)
    assert is_destructive(tool) is True


# ── require_confirmation() ─────────────────────────────────────────────


def test_require_confirmation_allows_safe_tools():
    def safe_tool():
        pass

    callback = require_confirmation()
    tool = FakeTool(name="safe_tool", func=safe_tool)
    ctx = FakeToolContext()

    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is None  # proceed


def test_require_confirmation_blocks_destructive_tool():
    @destructive("destroys data")
    def danger_tool():
        pass

    callback = require_confirmation()
    tool = FakeTool(name="danger_tool", func=danger_tool)
    ctx = FakeToolContext()

    result = callback(tool=tool, args={"name": "test"}, tool_context=ctx)
    assert result is not None
    assert result["status"] == "confirmation_required"
    assert "danger_tool" in result["message"]
    assert "destroys data" in result["message"]


def test_require_confirmation_allows_after_pending():
    @destructive("destroys data")
    def danger_tool():
        pass

    callback = require_confirmation()
    tool = FakeTool(name="danger_tool", func=danger_tool)
    ctx = FakeToolContext()

    # First call: blocked
    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is not None
    assert result["status"] == "confirmation_required"
    assert ctx.state["_guardrail_pending_danger_tool"] is True

    # Second call: allowed (user confirmed)
    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is None  # proceed
    assert ctx.state["_guardrail_pending_danger_tool"] is False


def test_require_confirmation_blocks_when_no_func():
    """If tool has no .func attribute, treat as safe."""
    callback = require_confirmation()
    tool = FakeTool(name="mystery", func=None)
    ctx = FakeToolContext()

    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is None


# ── dry_run() ──────────────────────────────────────────────────────────


def test_dry_run_allows_safe_tools():
    def safe_tool():
        pass

    callback = dry_run()
    tool = FakeTool(name="safe_tool", func=safe_tool)
    ctx = FakeToolContext()

    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is None


def test_dry_run_blocks_destructive_tool():
    @destructive("deletes data")
    def danger_tool():
        pass

    callback = dry_run()
    tool = FakeTool(name="danger_tool", func=danger_tool)
    ctx = FakeToolContext()

    result = callback(tool=tool, args={"id": 42}, tool_context=ctx)
    assert result is not None
    assert result["status"] == "dry_run"
    assert "DRY RUN" in result["message"]
    assert "danger_tool" in result["message"]


def test_dry_run_always_blocks_even_on_retry():
    @destructive("deletes data")
    def danger_tool():
        pass

    callback = dry_run()
    tool = FakeTool(name="danger_tool", func=danger_tool)
    ctx = FakeToolContext()

    # Always blocked, no confirmation mechanism
    result1 = callback(tool=tool, args={}, tool_context=ctx)
    result2 = callback(tool=tool, args={}, tool_context=ctx)
    assert result1["status"] == "dry_run"
    assert result2["status"] == "dry_run"
