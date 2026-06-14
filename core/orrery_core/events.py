"""Helpers for turning ADK runner events into user-facing text."""

from __future__ import annotations

from typing import Any


def extract_reply_text(event: Any) -> str:
    """Return the user-facing text from a single ADK runner event.

    Concatenates the text of every content part except Gemini "thinking"
    parts — emitted when a planner runs with ``include_thoughts`` (see
    ``resolve_planner``). Those parts carry the model's reasoning rather
    than the answer and must never reach the user, so every transport
    (Google Chat, Slack, HTTP, CLI) funnels event text through here.

    Returns ``""`` when the event carries no usable text.
    """
    content = getattr(event, "content", None)
    parts = getattr(content, "parts", None) if content else None
    if not parts:
        return ""

    text = ""
    for part in parts:
        # Gemini thought summaries are flagged ``thought=True``; skip them.
        if getattr(part, "thought", False):
            continue
        part_text = getattr(part, "text", None)
        if part_text:
            text += part_text
    return text
