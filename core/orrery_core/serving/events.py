"""Helpers for turning ADK runner events into user-facing text."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
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


@dataclass(frozen=True)
class TranscriptTurn:
    """One user or assistant message reconstructed from a session's events."""

    #: ``"user"`` or ``"assistant"``.
    role: str
    text: str
    #: Unix seconds, taken from the first event of the merged group.
    at: float


def _is_compaction(event: Any) -> bool:
    """True for a history-compaction digest rather than a real message.

    Compaction events (AEP-020) are authored ``"user"`` and keep their digest in
    ``actions.compaction.compacted_content``, not in ``content`` — so a
    text-only filter happens to skip them today. Checking explicitly means a
    future ADK that also populates ``content`` cannot inject an LLM summary of
    the conversation into the conversation as something the user said.
    """
    actions = getattr(event, "actions", None)
    return bool(actions is not None and getattr(actions, "compaction", None))


def build_transcript(events: Iterable[Any]) -> list[TranscriptTurn]:
    """Rebuild the user-visible conversation from a session's stored events.

    A session records far more than the transcript: function calls and
    responses, planner thoughts, and compaction digests all live in
    ``session.events``. This keeps only what the person said and what the agent
    said back, so a conversation reloaded from the store reads the way it did
    live.

    Consecutive same-role text is **merged into one message**, because one turn
    routinely emits several text events (a reply split around a tool call, a
    multi-part answer) and ``POST /chat`` returns their concatenation as a
    single reply. Merging here reproduces that instead of splitting one answer
    into several bubbles.
    """
    turns: list[TranscriptTurn] = []
    for event in events:
        if _is_compaction(event):
            continue
        text = extract_reply_text(event)
        if not text.strip():
            continue
        role = "user" if getattr(event, "author", "") == "user" else "assistant"
        at = float(getattr(event, "timestamp", 0.0) or 0.0)
        if turns and turns[-1].role == role:
            previous = turns[-1]
            turns[-1] = TranscriptTurn(role=role, text=previous.text + text, at=previous.at)
        else:
            turns.append(TranscriptTurn(role=role, text=text, at=at))
    return turns
