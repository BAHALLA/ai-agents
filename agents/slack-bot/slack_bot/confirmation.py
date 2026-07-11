"""Slack-aware confirmation callback for guarded tools.

Replaces the CLI-based require_confirmation() with Slack interactive buttons.
When a guarded tool is invoked, posts a Block Kit message with Approve/Deny
buttons. The user's button click is handled in app.py.
"""

from __future__ import annotations

import contextlib
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from google.adk.agents.context import Context
from google.adk.tools.base_tool import BaseTool

from orrery_core import (
    LEVEL_DESTRUCTIVE,
    get_guard_level,
    get_guard_reason,
)


@dataclass
class PendingConfirmation:
    """Stores context for a tool awaiting user approval."""

    action_id: str
    tool_name: str
    args: dict[str, Any]
    channel: str
    thread_ts: str
    session_id: str
    user_id: str
    level: str


@dataclass
class ConfirmationStore:
    """Thread-safe store for pending confirmations."""

    _pending: dict[str, PendingConfirmation] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, confirmation: PendingConfirmation) -> None:
        with self._lock:
            self._pending[confirmation.action_id] = confirmation

    def pop(self, action_id: str) -> PendingConfirmation | None:
        with self._lock:
            return self._pending.pop(action_id, None)

    def get(self, action_id: str) -> PendingConfirmation | None:
        with self._lock:
            return self._pending.get(action_id)


def approval_refusal(confirmation: PendingConfirmation, clicker: str | None) -> str | None:
    """Requester-only approval: the refusal message, or ``None`` when allowed.

    Fail-closed — an unidentifiable clicker, or one who is not the verified
    user that triggered the action, may not approve it. Deny is deliberately
    left open to anyone (an accidental deny is harmless; anyone should be able
    to stop a destructive action).
    """
    if not clicker or clicker != confirmation.user_id:
        return (
            f":no_entry: Approval refused: only the requester "
            f"(<@{confirmation.user_id}>) may approve `{confirmation.tool_name}`."
        )
    return None


def build_confirmation_blocks(
    tool_name: str,
    args: dict[str, Any],
    reason: str,
    level: str,
    action_id: str,
) -> list[dict]:
    """Build Slack Block Kit blocks for a confirmation prompt."""
    emoji = ":warning:" if level == LEVEL_DESTRUCTIVE else ":large_blue_circle:"
    level_label = "DESTRUCTIVE" if level == LEVEL_DESTRUCTIVE else "Confirmation Required"

    reason_text = f"\n> {reason}" if reason else ""
    args_text = ", ".join(f"`{k}={v}`" for k, v in args.items()) if args else "_none_"

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{emoji} *{level_label}*: `{tool_name}`{reason_text}\n*Arguments:* {args_text}"
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "style": "primary",
                    "action_id": f"confirm_{action_id}",
                    "value": action_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Deny"},
                    "style": "danger",
                    "action_id": f"deny_{action_id}",
                    "value": action_id,
                },
            ],
        },
    ]


def slack_confirmation(
    store: ConfirmationStore,
    slack_client: Any,
    channel_ref: dict[str, str],
) -> Callable:
    """Create a before_tool_callback that posts Slack buttons for guarded tools.

    Args:
        store: Shared ConfirmationStore for tracking pending approvals.
        slack_client: The Slack WebClient for posting messages.
        channel_ref: Mutable dict with 'channel' and 'thread_ts' keys,
            updated per-message by the handler so the callback knows
            where to post the buttons.
    """

    def callback(*, tool: BaseTool, args: dict[str, Any], tool_context: Context) -> dict | None:
        func = getattr(tool, "func", None)
        if func is None:
            return None

        level = get_guard_level(func)
        if level is None:
            return None  # not guarded, proceed

        # If already confirmed (pending flag set), allow through
        pending_key = f"_guardrail_pending_{tool.name}"
        if tool_context.state.get(pending_key):
            tool_context.state[pending_key] = False
            return None  # user confirmed via button, proceed

        # Block and post Slack buttons
        tool_context.state[pending_key] = True

        reason = get_guard_reason(func)
        action_id = uuid.uuid4().hex[:12]

        session_id = (
            tool_context.session.id
            if hasattr(tool_context, "session") and tool_context.session
            else "unknown"
        )
        user_id = getattr(tool_context, "user_id", "unknown")

        confirmation = PendingConfirmation(
            action_id=action_id,
            tool_name=tool.name,
            args=args,
            channel=channel_ref.get("channel", ""),
            thread_ts=channel_ref.get("thread_ts", ""),
            session_id=session_id,
            user_id=user_id,
            level=level,
        )
        store.add(confirmation)

        blocks = build_confirmation_blocks(tool.name, args, reason, level, action_id)

        with contextlib.suppress(Exception):
            slack_client.chat_postMessage(
                channel=channel_ref.get("channel", ""),
                thread_ts=channel_ref.get("thread_ts", ""),
                text=f"Confirmation required for `{tool.name}`",
                blocks=blocks,
            )

        reason_msg = f" This action {reason}." if reason else ""
        return {
            "status": "confirmation_required",
            "message": (
                f"The tool '{tool.name}' requires confirmation.{reason_msg} "
                f"A Slack approval button has been sent. "
                f"Waiting for user response."
            ),
        }

    return callback


def wire_tool_callbacks(root: Any, callbacks: list[Callable[..., Any]]) -> int:
    """Assign ``before_tool_callback`` to every tool-calling LlmAgent under *root*.

    Handles both an ``LlmAgent`` root (walks ``sub_agents`` + ``AgentTool``s) and
    a graph-based ``Workflow`` root (walks ``graph.nodes``), so Slack confirmation
    buttons fire for guarded tools no matter where the tool-calling agent lives.
    See ADR-003. Returns the number of agents wired.
    """
    seen: set[int] = set()
    wired = 0

    def visit(node: Any) -> None:
        nonlocal wired
        if node is None or id(node) in seen:
            return
        seen.add(id(node))

        tools = getattr(node, "tools", None)
        if tools is not None:
            node.before_tool_callback = callbacks
            wired += 1

        for sub in getattr(node, "sub_agents", None) or ():
            visit(sub)
        for tool in tools or ():
            inner = getattr(tool, "agent", None)
            if inner is not None:
                visit(inner)
        graph = getattr(node, "graph", None)
        if graph is not None:
            for gnode in getattr(graph, "nodes", None) or ():
                visit(gnode)

    visit(root)
    return wired
