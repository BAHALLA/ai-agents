"""Progressive-update tracker for live Google Chat progress cards.

As ADK runner events stream in during a background agent run, a
``ProgressTracker`` accumulates the pieces a progress card cares about:
which sub-agent is currently executing, the most recent tool-call
breadcrumb, subsystem health-check chips, and remediation-loop state.
``_handle_message_async`` consumes events, hands them to the tracker,
and fires a debounced async callback that PATCHes the initial
"Investigating…" card with a refreshed layout.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from orrery_core import extract_reply_text

from .cards import REMEDIATION_KEYS, SUBSYSTEMS, classify_status

logger = logging.getLogger("google_chat_bot.progress")

# Minimum interval between non-forced card flushes. Chat's update quota
# is generous but not free, and the LLM turn can emit many fine-grained
# events in bursts — throttle to keep the card visible-but-sane.
_DEBOUNCE_SECONDS = 0.8

_TRACKED_SUBSYSTEM_KEYS = {key for key, _ in SUBSYSTEMS}
_TRACKED_REMEDIATION_KEYS = set(REMEDIATION_KEYS)


class ProgressTracker:
    """Observe runner events and drive debounced progress-card updates."""

    def __init__(
        self,
        on_update: Callable[[ProgressTracker], Awaitable[None]] | None = None,
    ) -> None:
        self._on_update = on_update
        self._started_at = time.monotonic()
        self._last_flush = 0.0
        self._flush_lock = asyncio.Lock()
        self._pending_force = False
        self._dirty = False

        self.current_agent: str | None = None
        self.current_tool: str | None = None
        # Key → {"status": str, "summary": str}
        self.subsystem_chips: dict[str, dict[str, str]] = {}
        # Key → short summary string
        self.remediation_state: dict[str, str] = {}
        # Accumulated text across all events for the final reply.
        self.collected_text: str = ""
        # Full triage_report text once triage_summarizer writes it.
        self.triage_report: str | None = None

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._started_at

    async def consume(self, event: Any) -> None:
        """Incorporate a single runner event and flush if warranted."""
        force = False

        author = getattr(event, "author", None)
        if author and author != self.current_agent:
            self.current_agent = author
            self._dirty = True

        self.collected_text += extract_reply_text(event)

        for call in _safe_function_calls(event):
            self.current_tool = call.name
            self._dirty = True

        actions = getattr(event, "actions", None)
        state_delta: dict[str, Any] = getattr(actions, "state_delta", {}) or {}
        for key, value in state_delta.items():
            if key in _TRACKED_SUBSYSTEM_KEYS:
                status = classify_status(value if isinstance(value, str) else None)
                summary = _shorten(value) if isinstance(value, str) else ""
                self.subsystem_chips[key] = {"status": status, "summary": summary}
                if key == "kafka_status" or status != "pending":
                    force = True
                self._dirty = True
            elif key in _TRACKED_REMEDIATION_KEYS:
                self.remediation_state[key] = _shorten(str(value))
                force = True
                self._dirty = True
            elif key == "triage_report" and isinstance(value, str):
                self.triage_report = value
                force = True
                self._dirty = True

        if force:
            self._pending_force = True

        await self._maybe_flush()

    async def _maybe_flush(self) -> None:
        if not self._on_update or not self._dirty:
            return
        now = time.monotonic()
        if not self._pending_force and (now - self._last_flush) < _DEBOUNCE_SECONDS:
            return
        async with self._flush_lock:
            if not self._dirty:
                return
            self._dirty = False
            self._pending_force = False
            self._last_flush = time.monotonic()
            try:
                await self._on_update(self)
            except Exception:
                logger.exception("Progress update callback failed")

    async def flush_final(self) -> None:
        """Force one last update, e.g. at run completion."""
        if not self._on_update:
            return
        async with self._flush_lock:
            self._dirty = False
            self._pending_force = False
            self._last_flush = time.monotonic()
            try:
                await self._on_update(self)
            except Exception:
                logger.exception("Final progress update callback failed")


def _safe_function_calls(event: Any) -> list[Any]:
    getter = getattr(event, "get_function_calls", None)
    if not callable(getter):
        return []
    try:
        return list(getter() or [])
    except Exception:
        return []


def _shorten(text: str, max_chars: int = 180) -> str:
    """Pick a representative short summary from a status blob."""
    if not text:
        return ""
    for raw in text.splitlines():
        line = raw.strip()
        if line:
            return line if len(line) <= max_chars else line[: max_chars - 1] + "…"
    return ""
