"""Google Chat confirmation flow for guarded tools.

When a guarded tool fires, ``google_chat_confirmation`` short-circuits the
call, appends a Card v2 to a request-scoped buffer, and records the pending
action in a ``ConfirmationStore``. The handler returns the buffered card as
part of the synchronous webhook response — no Chat REST client needed. When
the user clicks Approve/Deny the handler marks the matching pending as
approved (or pops it on deny) and re-enters the runner with a synthetic
user message that includes the original arguments. On the retry the
callback consults the store and consumes the approved entry by
``(thread, tool_name, args_hash)`` — so the handshake survives an ADK
``AgentTool`` sub-agent that does not propagate per-call state to the
parent session.

This mirrors the Slack bot's confirmation pattern so operators see a
consistent UX across transports.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from google.adk.agents.context import Context
from google.adk.tools.base_tool import BaseTool

from orrery_core import get_guard_level, get_guard_reason

from .cards import build_confirmation_card

logger = logging.getLogger("google_chat_bot.confirmation")

_CONFIRMATION_TTL = 300  # seconds — pending entry retention
_APPROVAL_VALIDITY = 120  # seconds — window after Approve in which the retry must land


def _hash_args(args: dict[str, Any]) -> str:
    """Deterministic hash of tool arguments for confirmation matching."""
    canonical = json.dumps(args, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


# Per-request buffer for cards emitted by before_tool_callback. The handler
# sets this at the start of each webhook request; the callback appends to it
# and the handler returns the contents in the response.
_pending_cards: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "_gchat_pending_cards", default=None
)


@dataclass
class PendingConfirmation:
    """Stores context for a tool awaiting user approval."""

    action_id: str
    tool_name: str
    user_id: str
    session_id: str
    space_name: str
    thread_name: str | None
    level: str
    args: dict[str, Any] = field(default_factory=dict)
    args_hash: str = ""
    created_at: float = field(default_factory=time.time)
    approved: bool = False
    approved_at: float | None = None


@dataclass
class ConfirmationStore:
    """Thread-safe store of pending confirmations keyed by action_id."""

    _pending: dict[str, PendingConfirmation] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, confirmation: PendingConfirmation) -> None:
        with self._lock:
            self._prune_expired_locked()
            self._pending[confirmation.action_id] = confirmation

    def pop(self, action_id: str) -> PendingConfirmation | None:
        with self._lock:
            return self._pending.pop(action_id, None)

    def get(self, action_id: str) -> PendingConfirmation | None:
        with self._lock:
            return self._pending.get(action_id)

    def latest_for_thread(self, thread_or_space_key: str) -> PendingConfirmation | None:
        """Peek at the most-recently-added pending for this thread/space.

        No mutation — used to check *who* may decide before the Approve
        path marks the entry approved.
        """
        with self._lock:
            self._prune_expired_locked()
            for action_id in reversed(list(self._pending.keys())):
                pending = self._pending[action_id]
                if self._matches_thread_locked(pending, thread_or_space_key):
                    return pending
            return None

    def pop_latest_for_thread(self, thread_or_space_key: str) -> PendingConfirmation | None:
        """Pop the most-recently-added pending matching this thread/space.

        Used by the Deny flow — denial just discards the pending. The
        Approve flow uses :meth:`mark_latest_approved_for_thread` instead
        so the entry survives until the callback consumes it on retry.
        """
        with self._lock:
            self._prune_expired_locked()
            for action_id in reversed(list(self._pending.keys())):
                pending = self._pending[action_id]
                if self._matches_thread_locked(pending, thread_or_space_key):
                    return self._pending.pop(action_id)
            return None

    def mark_latest_approved_for_thread(
        self, thread_or_space_key: str
    ) -> PendingConfirmation | None:
        """Find the latest pending for this thread/space and mark approved.

        Does NOT pop — the entry stays in the store until the callback
        consumes it via :meth:`consume_approved` on the LLM's retry. This
        is the load-bearing primitive that makes the Approve flow work
        across an ``AgentTool`` boundary, where per-context state writes
        from the sub-agent don't propagate back to the parent session.
        """
        with self._lock:
            self._prune_expired_locked()
            for action_id in reversed(list(self._pending.keys())):
                pending = self._pending[action_id]
                if self._matches_thread_locked(pending, thread_or_space_key):
                    pending.approved = True
                    pending.approved_at = time.time()
                    return pending
            return None

    def consume_approved(
        self, thread_or_space_key: str, tool_name: str, args_hash: str
    ) -> PendingConfirmation | None:
        """Pop an approved pending matching ``(thread, tool, args_hash)``.

        The match must be both an exact ``args_hash`` and approved within
        ``_APPROVAL_VALIDITY``; stale approvals are ignored so a long-
        lingering entry can't auto-execute a fresh request later.
        """
        cutoff = time.time() - _APPROVAL_VALIDITY
        with self._lock:
            self._prune_expired_locked()
            for action_id, pending in list(self._pending.items()):
                if (
                    pending.approved
                    and pending.tool_name == tool_name
                    and pending.args_hash == args_hash
                    and (pending.approved_at or 0) >= cutoff
                    and self._matches_thread_locked(pending, thread_or_space_key)
                ):
                    return self._pending.pop(action_id)
            return None

    @staticmethod
    def _matches_thread_locked(pending: PendingConfirmation, key: str) -> bool:
        return pending.thread_name == key or pending.space_name == key

    def _prune_expired_locked(self) -> None:
        cutoff = time.time() - _CONFIRMATION_TTL
        for action_id in [k for k, v in self._pending.items() if v.created_at < cutoff]:
            self._pending.pop(action_id, None)


def start_request_buffer() -> tuple[list[dict[str, Any]], contextvars.Token]:
    """Begin a fresh card buffer for the current request context."""
    buf: list[dict[str, Any]] = []
    token = _pending_cards.set(buf)
    return buf, token


def end_request_buffer(token: contextvars.Token) -> None:
    """Tear down the per-request card buffer."""
    _pending_cards.reset(token)


def _push_card(card: dict[str, Any]) -> bool:
    """Append a card to the active request buffer. Returns False if none."""
    buf = _pending_cards.get()
    if buf is None:
        return False
    buf.append(card)
    return True


def apply_chat_confirmation(
    agent: Any, store: ConfirmationStore, *, interactive_buttons: bool = False
) -> int:
    """Apply :func:`google_chat_confirmation` to every LlmAgent in the tree.

    Walks ``agent``'s descendants — both ``sub_agents`` and ADK
    :class:`AgentTool`-wrapped agents in ``tools`` — and overrides each
    LlmAgent's ``before_tool_callback`` so guarded tools fire an
    interactive Card v2 regardless of which sub-agent invokes them.

    Without this, only the root agent's tools produce cards; tools on
    sub-agents (``k8s_health_agent.restart_deployment``, etc.) fall back
    to :func:`orrery_core.require_confirmation`, which asks the user to
    confirm via plain text — a regression from the Chat UX.

    Idempotent: replacing the callback on an already-wired agent is a
    no-op in effect (the closure carries the same store).

    Returns the number of agents that were wired, for logging.
    """
    callback = google_chat_confirmation(store, interactive_buttons=interactive_buttons)
    seen: set[int] = set()
    wired = 0

    def visit(node: Any) -> None:
        nonlocal wired
        if node is None or id(node) in seen:
            return
        seen.add(id(node))

        # Workflow agents (Sequential/Parallel/Loop) don't call tools
        # directly — only LlmAgents do — so we gate on the presence of
        # a ``tools`` attribute rather than a specific class check
        # (keeps the walker decoupled from ADK internals).
        tools = getattr(node, "tools", None)
        if tools is not None:
            node.before_tool_callback = callback
            wired += 1

        for sub in getattr(node, "sub_agents", None) or ():
            visit(sub)

        for tool in tools or ():
            inner = getattr(tool, "agent", None)
            if inner is not None:
                visit(inner)

        # Graph-based Workflow root (ADR-003): the tool-calling LlmAgents live
        # as nodes in ``graph.nodes`` rather than ``sub_agents``/``tools``.
        graph = getattr(node, "graph", None)
        if graph is not None:
            for gnode in getattr(graph, "nodes", None) or ():
                visit(gnode)

    visit(agent)
    return wired


def _resolve_parent_session_id(tool_context: Context) -> str:
    """Return the gchat parent runner session id.

    The handler writes ``gchat_thread`` and ``gchat_space`` into runner
    state at the start of each turn. We read them back here because
    ``tool_context.session.id`` reflects the inner ADK session — for a
    tool invoked through an ``AgentTool`` that's an ephemeral sub-agent
    session, not the gchat-keyed parent session the user is conversing
    in. Re-entering the runner on that ephemeral id loses all
    conversation history (see the regression that produced this fix).
    """
    state = getattr(tool_context, "state", None) or {}
    thread = state.get("gchat_thread") or None
    space = state.get("gchat_space", "")
    parent_key = thread or space
    if parent_key:
        return f"gchat:{parent_key}"
    if hasattr(tool_context, "session") and tool_context.session:
        return tool_context.session.id
    return "unknown"


def google_chat_confirmation(
    store: ConfirmationStore, *, interactive_buttons: bool = False
) -> Callable:
    """Create a ``before_tool_callback`` that emits approval cards.

    Args:
        store: Shared :class:`ConfirmationStore` used to resume runs when
            the user clicks Approve.
    """

    def callback(*, tool: BaseTool, args: dict[str, Any], tool_context: Context) -> dict | None:
        func = getattr(tool, "func", None)
        if func is None:
            return None

        level = get_guard_level(func)
        if level is None:
            return None  # not guarded, proceed

        state = getattr(tool_context, "state", None) or {}
        space_name = state.get("gchat_space", "") or ""
        thread_name = state.get("gchat_thread") or None
        thread_key = thread_name or space_name

        args_hash = _hash_args(args)

        # Approve flow: an entry in the store for this (thread, tool, args)
        # that the click handler has marked approved consumes here and
        # lets the call through. ``consume_approved`` enforces a short
        # validity window so a stale approval can't auto-execute a fresh
        # request long after the operator clicked.
        if thread_key:
            approved = store.consume_approved(thread_key, tool.name, args_hash)
            if approved is not None:
                logger.info(
                    "Consumed approval for tool=%s args_hash=%s thread=%s",
                    tool.name,
                    args_hash,
                    thread_key,
                )
                return None

        # Block: register a fresh pending and emit a card.
        reason = get_guard_reason(func)
        action_id = uuid.uuid4().hex[:12]
        user_id = getattr(tool_context, "user_id", "unknown")

        store.add(
            PendingConfirmation(
                action_id=action_id,
                tool_name=tool.name,
                user_id=user_id,
                session_id=_resolve_parent_session_id(tool_context),
                space_name=space_name,
                thread_name=thread_name,
                level=level,
                args=dict(args),
                args_hash=args_hash,
            )
        )

        card = build_confirmation_card(
            tool.name, args, reason, level, action_id, interactive_buttons=interactive_buttons
        )
        buffered = _push_card(card)

        reason_msg = f" This action {reason}." if reason else ""
        notice = (
            (
                "An approval card has been posted — click Approve or Deny."
                if interactive_buttons
                else (
                    "An approval card has been posted — the user must reply "
                    "'approve' or 'deny' in this thread."
                )
            )
            if buffered
            else "Approval is required from an operator."
        )
        return {
            "status": "confirmation_required",
            "message": (
                f"The tool '{tool.name}' requires confirmation.{reason_msg} "
                f"{notice} Waiting for user response."
            ),
        }

    return callback
