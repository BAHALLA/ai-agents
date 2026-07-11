"""Guardrails for tool execution safety.

Provides before_tool_callback factories that gate operations requiring confirmation.
Tools are classified with two levels:
  - @destructive("reason") — dangerous, irreversible operations (delete, drop, etc.)
  - @confirm("reason")     — mutating but non-destructive operations (create, update, etc.)

Unmarked tools are treated as safe and execute immediately.

Two confirmation modes:
  - **Model-mediated (default)** — the gate blocks the first call and tells the
    model to ask the user; a re-call with the same args in a *new* invocation is
    treated as confirmed. Simple, works everywhere, but ultimately trusts the
    model to actually have asked.
  - **Requester-verified (strict)** — active when a transport stamps
    ``_confirmation_strict`` into session state (see
    ``AgentGateway(verified_confirmation=True)``). The gate additionally
    requires a *human* decision: the gateway records an explicit approval
    message (a deliberate word — "approve"/"confirm"/"proceed"/… — a casual
    "ok" doesn't count) stamped with the sender, and the gate only passes when
    that sender is the same verified actor who triggered the pending action.
    Fail-closed: no decision, an unknown requester, or a second person's
    approval all refuse.

ADK calls before_tool_callback with keyword args:
    callback(tool=..., args=..., tool_context=...)
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from typing import Any

from google.adk.agents.context import Context
from google.adk.tools.base_tool import BaseTool

_CONFIRMATION_TTL = 300  # 5 minutes

# ── Identity / decision state keys ─────────────────────────────────────

#: Session-state key carrying the identity of the person making the current
#: request. Stamped per turn by the serving gateway from the transport's
#: verified user id; also read by the identity-aware instruction provider.
ACTOR_STATE_KEY = "actor"

#: Set truthy by a transport to arm requester-verified (strict) confirmation.
CONFIRMATION_STRICT_STATE_KEY = "_confirmation_strict"

#: Per-turn human decision stamped by the gateway:
#: ``{"decision": "approve"|"deny", "by": <user_id>, "timestamp": <epoch>}``.
CONFIRMATION_DECISION_STATE_KEY = "_confirmation_decision"

# Approve needs a deliberate word so a casual "ok"/"yes" can't authorize a
# destructive action; deny is broad since an accidental deny is harmless.
_APPROVE_PHRASES = frozenset(
    {"approve", "approved", "confirm", "confirmed", "proceed", "accept", "go ahead"}
)
_DENY_WORDS = frozenset(
    {
        "no",
        "deny",
        "denied",
        "cancel",
        "cancelled",
        "stop",
        "abort",
        "reject",
        "rejected",
        "dont",
        "don",  # "don't" — the apostrophe is stripped by normalization
    }
)


def classify_decision(text: str) -> str | None:
    """Classify a user message as a confirmation decision, or ``None``.

    ``"approve"`` only when the whole (normalized) message is a deliberate
    approval phrase; ``"deny"`` when it opens with a deny word. Anything else
    is not a decision and flows to the agent unchanged.
    """
    tokens = re.sub(r"[^\w\s]", " ", text.lower()).split()
    if not tokens:
        return None
    if " ".join(tokens) in _APPROVE_PHRASES:
        return "approve"
    if tokens[0] in _DENY_WORDS:
        return "deny"
    return None


def _state_actor(state: Any) -> str | None:
    """The verified actor for the current turn, from session state."""
    get = getattr(state, "get", None)
    if not callable(get):
        return None
    actor = get(ACTOR_STATE_KEY)
    if actor:
        return str(actor)
    auth = get("_auth")
    subject = auth.get("subject") if isinstance(auth, dict) else None
    return str(subject) if subject else None


# ── Tool classification markers ────────────────────────────────────────

_GUARD_LEVEL_ATTR = "_guardrail_level"
_GUARD_REASON_ATTR = "_guardrail_reason"

LEVEL_CONFIRM = "confirm"
LEVEL_DESTRUCTIVE = "destructive"


def confirm(reason: str = "") -> Callable:
    """Mark a tool as requiring user confirmation before execution.

    Use this for mutating but non-destructive operations (create, update, scale).

    Args:
        reason: Explanation shown to the user (e.g., "creates a new topic").

    Usage:
        @confirm("creates a new topic on the cluster")
        def create_kafka_topic(topic_name: str) -> dict:
            ...
    """

    def decorator(func: Callable) -> Callable:
        setattr(func, _GUARD_LEVEL_ATTR, LEVEL_CONFIRM)
        setattr(func, _GUARD_REASON_ATTR, reason)
        return func

    return decorator


def destructive(reason: str = "") -> Callable:
    """Mark a tool as destructive, requiring user confirmation before execution.

    Use this for dangerous, irreversible operations (delete, drop, purge).

    Args:
        reason: Explanation shown to the user
                (e.g., "permanently deletes the topic and all its data").

    Usage:
        @destructive("permanently deletes the topic and all its data")
        def delete_kafka_topic(topic_name: str) -> dict:
            ...
    """

    def decorator(func: Callable) -> Callable:
        setattr(func, _GUARD_LEVEL_ATTR, LEVEL_DESTRUCTIVE)
        setattr(func, _GUARD_REASON_ATTR, reason)
        return func

    return decorator


def get_guard_level(tool_or_func: Any) -> str | None:
    """Get the guardrail level of a tool or function."""
    func = getattr(tool_or_func, "func", tool_or_func)
    return getattr(func, _GUARD_LEVEL_ATTR, None)


def get_guard_reason(tool_or_func: Any) -> str:
    """Get the guardrail reason of a tool or function."""
    func = getattr(tool_or_func, "func", tool_or_func)
    return getattr(func, _GUARD_REASON_ATTR, "")


def is_destructive(tool_or_func: Any) -> bool:
    """Check if a tool or function is marked as destructive."""
    return get_guard_level(tool_or_func) == LEVEL_DESTRUCTIVE


def is_guarded(tool_or_func: Any) -> bool:
    """Check if a tool or function requires any confirmation."""
    return get_guard_level(tool_or_func) is not None


# ── Helpers ────────────────────────────────────────────────────────────


def _hash_args(args: dict[str, Any]) -> str:
    """Deterministic hash of tool arguments for confirmation matching."""
    canonical = json.dumps(args, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


# ── Callback factories ─────────────────────────────────────────────────


def require_confirmation() -> Callable:
    """Create a before_tool_callback that gates guarded tools.

    - @destructive tools get a warning: "This is a destructive operation..."
    - @confirm tools get a neutral prompt: "This operation will..."
    - Unmarked tools execute immediately.

    The confirmation state tracks the exact arguments and expires after
    ``_CONFIRMATION_TTL`` seconds.  A retry with different arguments or
    an expired confirmation will re-prompt.

    Usage in create_agent():
        create_agent(
            ...,
            before_tool_callback=require_confirmation(),
        )
    """

    def callback(*, tool: BaseTool, args: dict[str, Any], tool_context: Context) -> dict | None:
        func = getattr(tool, "func", None)
        if func is None:
            return None

        level = get_guard_level(func)
        if level is None:
            return None  # not guarded, proceed

        pending_key = f"_guardrail_pending_{tool.name}"
        pending = tool_context.state.get(pending_key)
        args_hash = _hash_args(args)

        # Identify the current invocation so we can distinguish LLM
        # auto-retries (same invocation) from user-confirmed retries
        # (new invocation triggered by a new user message or AgentTool call).
        invocation_id = getattr(
            getattr(tool_context, "_invocation_context", None),
            "invocation_id",
            None,
        )

        strict = bool(tool_context.state.get(CONFIRMATION_STRICT_STATE_KEY))

        # Check for a valid pending confirmation that matches these args.
        if isinstance(pending, dict):
            same_args = pending.get("args_hash") == args_hash
            not_expired = (time.time() - pending.get("timestamp", 0)) < _CONFIRMATION_TTL
            different_invocation = pending.get("invocation_id") != invocation_id

            if strict:
                # Requester-verified mode: a re-call alone proves nothing — an
                # explicit human decision must have been stamped this turn, by
                # the same verified actor who triggered the pending action.
                decision = tool_context.state.get(CONFIRMATION_DECISION_STATE_KEY)
                decision = decision if isinstance(decision, dict) else {}
                decision_fresh = (time.time() - decision.get("timestamp", 0)) < _CONFIRMATION_TTL
                requester = pending.get("requester")
                approved = (
                    decision.get("decision") == "approve"
                    and decision_fresh
                    and bool(requester)  # fail-closed on unknown requester
                    and decision.get("by") == requester
                )
                if same_args and not_expired and approved:
                    tool_context.state[pending_key] = None  # consume both
                    tool_context.state[CONFIRMATION_DECISION_STATE_KEY] = None
                    return None  # verified requester approved, proceed
                if same_args and decision.get("decision") == "deny" and decision_fresh:
                    tool_context.state[pending_key] = None
                    tool_context.state[CONFIRMATION_DECISION_STATE_KEY] = None
                    return {
                        "status": "denied",
                        "message": (
                            f"The user denied the pending '{tool.name}' operation. "
                            f"Do not retry it unless the user asks again."
                        ),
                    }
                if decision.get("decision") == "approve" and same_args and not_expired:
                    # An approval arrived but from someone other than the
                    # requester (or the requester is unknown) — refuse, keep
                    # the pending so the real requester can still decide.
                    return {
                        "status": "confirmation_required",
                        "message": (
                            f"The approval for '{tool.name}' was not given by the person "
                            f"who requested it, so it was refused. Only the original "
                            f"requester may approve. Ask them to reply 'approve' or 'deny'."
                        ),
                    }
                # No/stale decision, args mismatch, or expired — clear what's
                # invalid and fall through to (re-)prompt.
                if not same_args or not not_expired:
                    tool_context.state[pending_key] = None
            else:
                if same_args and not_expired and different_invocation:
                    tool_context.state[pending_key] = None  # consume confirmation
                    return None  # user confirmed, proceed
                # Same-invocation retry, args mismatch, or expired — re-prompt.
                if not same_args or not not_expired:
                    tool_context.state[pending_key] = None

        # Block and store pending with args fingerprint + timestamp. In strict
        # mode the pending also pins the requester so only that verified actor
        # can approve it later.
        tool_context.state[pending_key] = {
            "args_hash": args_hash,
            "timestamp": time.time(),
            "invocation_id": invocation_id,
            "requester": _state_actor(tool_context.state),
        }

        reason = get_guard_reason(func)
        reason_msg = f" This action {reason}." if reason else ""
        classification = (
            "is a destructive operation" if level == LEVEL_DESTRUCTIVE else "requires confirmation"
        )
        how_to_confirm = (
            "The user must reply with an explicit 'approve' (or 'deny') — a casual "
            "'yes' will not authorize it. After they decide, call the tool again."
            if strict
            else "If the user confirms, call the tool again."
        )
        message = (
            f"The tool '{tool.name}' {classification}.{reason_msg} "
            f"Please confirm with the user before proceeding. "
            f"Arguments: {args}. "
            f"{how_to_confirm}"
        )

        return {"status": "confirmation_required", "message": message}

    return callback


def dry_run() -> Callable:
    """Create a before_tool_callback that blocks ALL guarded tools.

    Guarded tools are never executed — instead, a dry-run message is
    returned showing what would have been done.

    Usage:
        create_agent(..., before_tool_callback=dry_run())
    """

    def callback(*, tool: BaseTool, args: dict[str, Any], tool_context: Context) -> dict | None:
        func = getattr(tool, "func", None)
        if func is None or not is_guarded(func):
            return None

        reason = get_guard_reason(func)
        return {
            "status": "dry_run",
            "message": (
                f"[DRY RUN] Would have called '{tool.name}' with args: {args}. "
                f"{'Reason it is gated: ' + reason + '. ' if reason else ''}"
                f"No changes were made."
            ),
        }

    return callback
