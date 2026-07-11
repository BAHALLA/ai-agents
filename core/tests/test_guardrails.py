"""Tests for orrery_core.security.guardrails."""

from __future__ import annotations

import time

from hypothesis import given
from hypothesis import strategies as st

from orrery_core.security.guardrails import (
    _CONFIRMATION_TTL,
    ACTOR_STATE_KEY,
    CONFIRMATION_DECISION_STATE_KEY,
    CONFIRMATION_STRICT_STATE_KEY,
    _hash_args,
    classify_decision,
    confirm,
    destructive,
    dry_run,
    get_guard_reason,
    is_destructive,
    is_guarded,
    require_confirmation,
)

# ── @destructive decorator ─────────────────────────────────────────────


def test_destructive_marks_function():
    @destructive("deletes everything")
    def my_tool():
        pass

    assert is_destructive(my_tool) is True
    assert is_guarded(my_tool) is True
    assert get_guard_reason(my_tool) == "deletes everything"


def test_unmarked_function_is_not_destructive():
    def safe_tool():
        pass

    assert is_destructive(safe_tool) is False
    assert is_guarded(safe_tool) is False


def test_destructive_with_empty_reason():
    @destructive()
    def my_tool():
        pass

    assert is_destructive(my_tool) is True
    assert get_guard_reason(my_tool) == ""


def test_is_destructive_checks_func_attr(fake_tool):
    """ADK wraps functions in BaseTool objects with a .func attribute."""

    @destructive("reason")
    def my_func():
        pass

    tool = fake_tool(name="my_func", func=my_func)
    assert is_destructive(tool) is True


# ── @confirm decorator ─────────────────────────────────────────────────


def test_confirm_marks_function():
    @confirm("creates a resource")
    def my_tool():
        pass

    assert is_guarded(my_tool) is True
    assert is_destructive(my_tool) is False


def test_confirm_stores_reason():
    @confirm("creates a new topic")
    def my_tool():
        pass

    assert get_guard_reason(my_tool) == "creates a new topic"


# ── require_confirmation() ─────────────────────────────────────────────


def test_require_confirmation_allows_safe_tools(fake_tool, fake_ctx):
    def safe_tool():
        pass

    callback = require_confirmation()
    tool = fake_tool(name="safe_tool", func=safe_tool)
    ctx = fake_ctx()

    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is None  # proceed


def test_require_confirmation_blocks_destructive_tool(fake_tool, fake_ctx):
    @destructive("destroys data")
    def danger_tool():
        pass

    callback = require_confirmation()
    tool = fake_tool(name="danger_tool", func=danger_tool)
    ctx = fake_ctx()

    result = callback(tool=tool, args={"name": "test"}, tool_context=ctx)
    assert result is not None
    assert result["status"] == "confirmation_required"
    assert "destructive" in result["message"]
    assert "destroys data" in result["message"]


def test_require_confirmation_blocks_confirm_tool_with_neutral_message(fake_tool, fake_ctx):
    @confirm("creates a new topic on the cluster")
    def create_tool():
        pass

    callback = require_confirmation()
    tool = fake_tool(name="create_tool", func=create_tool)
    ctx = fake_ctx()

    result = callback(tool=tool, args={"name": "test"}, tool_context=ctx)
    assert result is not None
    assert result["status"] == "confirmation_required"
    assert "requires confirmation" in result["message"]
    assert "destructive" not in result["message"]
    assert "creates a new topic" in result["message"]


def test_require_confirmation_allows_after_pending(fake_tool, fake_ctx):
    @destructive("destroys data")
    def danger_tool():
        pass

    callback = require_confirmation()
    tool = fake_tool(name="danger_tool", func=danger_tool)
    ctx = fake_ctx(invocation_id="inv-1")

    # First call: blocked
    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is not None
    assert result["status"] == "confirmation_required"
    pending = ctx.state["_guardrail_pending_danger_tool"]
    assert isinstance(pending, dict)
    assert "args_hash" in pending
    assert "timestamp" in pending
    assert "invocation_id" in pending

    # Simulate new invocation (user confirmed and agent retries)
    ctx._invocation_context.invocation_id = "inv-2"

    # Second call with same args from different invocation: allowed
    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is None  # proceed
    assert ctx.state["_guardrail_pending_danger_tool"] is None


def test_require_confirmation_blocks_same_invocation_retry(fake_tool, fake_ctx):
    """LLM auto-retry within the same invocation must NOT bypass confirmation."""

    @destructive("destroys data")
    def danger_tool():
        pass

    callback = require_confirmation()
    tool = fake_tool(name="danger_tool", func=danger_tool)
    ctx = fake_ctx(invocation_id="inv-1")

    # First call: blocked
    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result["status"] == "confirmation_required"

    # Same invocation retry: must block again
    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is not None
    assert result["status"] == "confirmation_required"


def test_require_confirmation_allows_confirm_tool_after_pending(fake_tool, fake_ctx):
    @confirm("creates a resource")
    def create_tool():
        pass

    callback = require_confirmation()
    tool = fake_tool(name="create_tool", func=create_tool)
    ctx = fake_ctx(invocation_id="inv-1")

    # First call: blocked
    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is not None

    # Simulate new invocation
    ctx._invocation_context.invocation_id = "inv-2"

    # Second call: allowed
    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is None


def test_require_confirmation_blocks_when_no_func(fake_tool, fake_ctx):
    """If tool has no .func attribute, treat as safe."""
    callback = require_confirmation()
    tool = fake_tool(name="mystery", func=None)
    ctx = fake_ctx()

    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is None


# ── dry_run() ──────────────────────────────────────────────────────────


def test_dry_run_allows_safe_tools(fake_tool, fake_ctx):
    def safe_tool():
        pass

    callback = dry_run()
    tool = fake_tool(name="safe_tool", func=safe_tool)
    ctx = fake_ctx()

    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is None


def test_dry_run_blocks_destructive_tool(fake_tool, fake_ctx):
    @destructive("deletes data")
    def danger_tool():
        pass

    callback = dry_run()
    tool = fake_tool(name="danger_tool", func=danger_tool)
    ctx = fake_ctx()

    result = callback(tool=tool, args={"id": 42}, tool_context=ctx)
    assert result is not None
    assert result["status"] == "dry_run"
    assert "DRY RUN" in result["message"]


def test_dry_run_blocks_confirm_tool(fake_tool, fake_ctx):
    @confirm("creates a resource")
    def create_tool():
        pass

    callback = dry_run()
    tool = fake_tool(name="create_tool", func=create_tool)
    ctx = fake_ctx()

    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is not None
    assert result["status"] == "dry_run"


def test_dry_run_always_blocks_even_on_retry(fake_tool, fake_ctx):
    @destructive("deletes data")
    def danger_tool():
        pass

    callback = dry_run()
    tool = fake_tool(name="danger_tool", func=danger_tool)
    ctx = fake_ctx()

    result1 = callback(tool=tool, args={}, tool_context=ctx)
    result2 = callback(tool=tool, args={}, tool_context=ctx)
    assert result1["status"] == "dry_run"
    assert result2["status"] == "dry_run"


# ── Confirmation bypass prevention ────────────────────────────────────


def test_require_confirmation_rejects_different_args(fake_tool, fake_ctx):
    """Changing args on retry must re-prompt, not silently pass through."""

    @destructive("destroys data")
    def danger_tool():
        pass

    callback = require_confirmation()
    tool = danger_tool
    if callable(fake_tool):
        tool = fake_tool(name="danger_tool", func=danger_tool)
    ctx = fake_ctx()

    # First call with args_a: blocked
    result = callback(tool=tool, args={"name": "topic-a"}, tool_context=ctx)
    assert result["status"] == "confirmation_required"

    # Second call with different args: must block again
    result = callback(tool=tool, args={"name": "topic-b"}, tool_context=ctx)
    assert result is not None
    assert result["status"] == "confirmation_required"


def test_require_confirmation_expired_pending(fake_tool, fake_ctx):
    """An expired pending confirmation must re-prompt."""

    @destructive("destroys data")
    def danger_tool():
        pass

    callback = require_confirmation()
    tool = fake_tool(name="danger_tool", func=danger_tool)
    ctx = fake_ctx(invocation_id="inv-1")

    # First call: blocked
    callback(tool=tool, args={"id": 1}, tool_context=ctx)

    # Manually expire the pending state
    pending_key = "_guardrail_pending_danger_tool"
    ctx.state[pending_key]["timestamp"] = time.time() - _CONFIRMATION_TTL - 10

    # New invocation, same args but expired: should block again
    ctx._invocation_context.invocation_id = "inv-2"
    result = callback(tool=tool, args={"id": 1}, tool_context=ctx)
    assert result is not None
    assert result["status"] == "confirmation_required"


def test_require_confirmation_clears_stale_on_mismatch(fake_tool, fake_ctx):
    """On args mismatch the old pending is replaced with new pending."""

    @confirm("creates resource")
    def create_tool():
        pass

    callback = require_confirmation()
    tool = fake_tool(name="create_tool", func=create_tool)
    ctx = fake_ctx()

    # Block with args_a
    callback(tool=tool, args={"name": "a"}, tool_context=ctx)
    pending_key = "_guardrail_pending_create_tool"
    old_hash = ctx.state[pending_key]["args_hash"]

    # Call with args_b — should block again with new hash
    callback(tool=tool, args={"name": "b"}, tool_context=ctx)
    new_hash = ctx.state[pending_key]["args_hash"]
    assert old_hash != new_hash
    assert new_hash == _hash_args({"name": "b"})


def test_require_confirmation_handles_legacy_boolean_pending(fake_tool, fake_ctx):
    """Old boolean pending state is treated as invalid and re-prompts."""

    @destructive("destroys data")
    def danger_tool():
        pass

    callback = require_confirmation()
    tool = fake_tool(name="danger_tool", func=danger_tool)
    ctx = fake_ctx()

    # Simulate legacy boolean state from older code
    ctx.state["_guardrail_pending_danger_tool"] = True

    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result["status"] == "confirmation_required"


# ── classify_decision ─────────────────────────────────────────────────


def test_classify_decision_approve_needs_deliberate_word():
    assert classify_decision("approve") == "approve"
    assert classify_decision("Approve!") == "approve"
    assert classify_decision("CONFIRM") == "approve"
    assert classify_decision("go ahead") == "approve"
    # Casual assent must NOT authorize.
    assert classify_decision("ok") is None
    assert classify_decision("yes") is None
    assert classify_decision("sure, sounds good") is None
    # A decision word embedded in a longer request is not a decision.
    assert classify_decision("approve the MR after tests pass") is None


def test_classify_decision_deny_is_broad():
    for text in ("no", "No way", "cancel", "stop it", "abort", "don't do that"):
        assert classify_decision(text) == "deny", text
    assert classify_decision("delete the topic") is None
    assert classify_decision("") is None


# ── Requester-verified (strict) confirmation ──────────────────────────


def _strict_ctx(fake_ctx, actor="alice@example.com", invocation_id="inv-1"):
    return fake_ctx(
        state={CONFIRMATION_STRICT_STATE_KEY: True, ACTOR_STATE_KEY: actor},
        invocation_id=invocation_id,
    )


def _stamp_decision(ctx, decision, by, ts=None):
    ctx.state[CONFIRMATION_DECISION_STATE_KEY] = {
        "decision": decision,
        "by": by,
        "timestamp": ts if ts is not None else time.time(),
    }


def _danger(fake_tool):
    @destructive("destroys data")
    def danger_tool():
        pass

    return fake_tool(name="danger_tool", func=danger_tool)


def test_strict_records_requester_on_pending(fake_tool, fake_ctx):
    callback = require_confirmation()
    ctx = _strict_ctx(fake_ctx)

    result = callback(tool=_danger(fake_tool), args={}, tool_context=ctx)
    assert result["status"] == "confirmation_required"
    assert "'approve'" in result["message"]
    assert ctx.state["_guardrail_pending_danger_tool"]["requester"] == "alice@example.com"


def test_strict_recall_without_decision_stays_blocked(fake_tool, fake_ctx):
    """A model re-call alone (new invocation, same args) must NOT pass in strict mode."""
    callback = require_confirmation()
    ctx = _strict_ctx(fake_ctx)
    tool = _danger(fake_tool)

    callback(tool=tool, args={}, tool_context=ctx)
    ctx._invocation_context.invocation_id = "inv-2"  # new invocation, no human decision

    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is not None
    assert result["status"] == "confirmation_required"


def test_strict_requester_approval_passes_and_consumes(fake_tool, fake_ctx):
    callback = require_confirmation()
    ctx = _strict_ctx(fake_ctx)
    tool = _danger(fake_tool)

    callback(tool=tool, args={"id": 1}, tool_context=ctx)
    _stamp_decision(ctx, "approve", by="alice@example.com")
    ctx._invocation_context.invocation_id = "inv-2"

    assert callback(tool=tool, args={"id": 1}, tool_context=ctx) is None
    assert ctx.state["_guardrail_pending_danger_tool"] is None
    assert ctx.state[CONFIRMATION_DECISION_STATE_KEY] is None  # consumed


def test_strict_second_person_approval_refused(fake_tool, fake_ctx):
    """Only the verified actor who triggered the action may approve it."""
    callback = require_confirmation()
    ctx = _strict_ctx(fake_ctx, actor="alice@example.com")
    tool = _danger(fake_tool)

    callback(tool=tool, args={}, tool_context=ctx)
    _stamp_decision(ctx, "approve", by="mallory@example.com")

    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is not None
    assert result["status"] == "confirmation_required"
    assert "not given by the person" in result["message"]
    # The pending survives so the real requester can still decide.
    assert isinstance(ctx.state["_guardrail_pending_danger_tool"], dict)


def test_strict_unknown_requester_fails_closed(fake_tool, fake_ctx):
    """No verified actor at request time → nobody can approve."""
    callback = require_confirmation()
    ctx = fake_ctx(state={CONFIRMATION_STRICT_STATE_KEY: True})  # no actor
    tool = _danger(fake_tool)

    callback(tool=tool, args={}, tool_context=ctx)
    assert ctx.state["_guardrail_pending_danger_tool"]["requester"] is None
    _stamp_decision(ctx, "approve", by="anyone@example.com")

    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is not None  # still blocked


def test_strict_deny_clears_pending(fake_tool, fake_ctx):
    callback = require_confirmation()
    ctx = _strict_ctx(fake_ctx)
    tool = _danger(fake_tool)

    callback(tool=tool, args={}, tool_context=ctx)
    _stamp_decision(ctx, "deny", by="alice@example.com")

    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result["status"] == "denied"
    assert ctx.state["_guardrail_pending_danger_tool"] is None


def test_strict_stale_approval_refused(fake_tool, fake_ctx):
    callback = require_confirmation()
    ctx = _strict_ctx(fake_ctx)
    tool = _danger(fake_tool)

    callback(tool=tool, args={}, tool_context=ctx)
    _stamp_decision(ctx, "approve", by="alice@example.com", ts=time.time() - _CONFIRMATION_TTL - 10)

    result = callback(tool=tool, args={}, tool_context=ctx)
    assert result is not None
    assert result["status"] == "confirmation_required"


def test_strict_approval_wrong_args_reprompts(fake_tool, fake_ctx):
    """An approval must authorize exactly the pending args, nothing else."""
    callback = require_confirmation()
    ctx = _strict_ctx(fake_ctx)
    tool = _danger(fake_tool)

    callback(tool=tool, args={"name": "topic-a"}, tool_context=ctx)
    _stamp_decision(ctx, "approve", by="alice@example.com")

    result = callback(tool=tool, args={"name": "topic-b"}, tool_context=ctx)
    assert result is not None
    assert result["status"] == "confirmation_required"


def test_non_strict_flow_unchanged_by_new_fields(fake_tool, fake_ctx):
    """Without the strict flag, the model-mediated flow behaves as before."""
    callback = require_confirmation()
    ctx = fake_ctx(invocation_id="inv-1")
    tool = _danger(fake_tool)

    assert callback(tool=tool, args={}, tool_context=ctx)["status"] == "confirmation_required"
    ctx._invocation_context.invocation_id = "inv-2"
    assert callback(tool=tool, args={}, tool_context=ctx) is None


# ── Hypothesis Property-Based Tests ────────────────────────────────────


@given(st.dictionaries(st.text(), st.text()))
def test_hash_args_determinism(args):
    """Hash must be the same for the same input."""
    assert _hash_args(args) == _hash_args(args)


@given(st.dictionaries(st.text(), st.text()))
def test_hash_args_key_ordering_invariance(args):
    """Hash must be invariant under key reordering."""
    if len(args) < 2:
        return
    keys = list(args.keys())
    # Create a dictionary with a different insertion order
    shuffled_args = {k: args[k] for k in reversed(keys)}
    assert _hash_args(args) == _hash_args(shuffled_args)


@given(st.dictionaries(st.text(), st.one_of(st.text(), st.integers(), st.booleans(), st.none())))
def test_hash_args_handles_diverse_types(args):
    """Hash must handle standard JSON-serializable types + default str fallback."""
    h = _hash_args(args)
    assert isinstance(h, str)
    assert len(h) == 16
