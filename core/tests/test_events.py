"""Tests for orrery_core.serving.events (reply text + transcript rebuild)."""

from __future__ import annotations

from unittest.mock import MagicMock

from orrery_core import extract_reply_text
from orrery_core.serving.events import build_transcript


def _part(text: str | None, *, thought: bool | None = None) -> MagicMock:
    part = MagicMock()
    part.text = text
    part.thought = thought
    return part


def _event(
    parts: list | None,
    *,
    author: str = "model",
    timestamp: float = 0.0,
    compaction: object | None = None,
) -> MagicMock:
    event = MagicMock()
    if parts is None:
        event.content = None
    else:
        content = MagicMock()
        content.parts = parts
        event.content = content
    event.author = author
    event.timestamp = timestamp
    event.actions.compaction = compaction
    return event


def test_returns_empty_for_no_content():
    assert extract_reply_text(_event(None)) == ""


def test_returns_empty_for_no_parts():
    assert extract_reply_text(_event([])) == ""


def test_concatenates_text_parts():
    event = _event([_part("Hello "), _part("world.")])
    assert extract_reply_text(event) == "Hello world."


def test_skips_thought_parts():
    event = _event([_part("reasoning...", thought=True), _part("The answer.")])
    assert extract_reply_text(event) == "The answer."


def test_skips_empty_text_parts():
    event = _event([_part(None), _part(""), _part("real")])
    assert extract_reply_text(event) == "real"


def test_all_thoughts_yields_empty():
    event = _event([_part("a", thought=True), _part("b", thought=True)])
    assert extract_reply_text(event) == ""


# ── build_transcript ─────────────────────────────────────────────────


def test_transcript_pairs_user_and_assistant_turns():
    turns = build_transcript(
        [
            _event([_part("kafka health?")], author="user", timestamp=100.0),
            _event([_part("All brokers up.")], author="orrery_chat_agent", timestamp=101.0),
        ]
    )
    assert [(t.role, t.text, t.at) for t in turns] == [
        ("user", "kafka health?", 100.0),
        ("assistant", "All brokers up.", 101.0),
    ]


def test_transcript_merges_one_turn_split_across_events():
    """A reply split around a tool call is one message, as /chat returned it."""
    turns = build_transcript(
        [
            _event([_part("check kafka")], author="user", timestamp=1.0),
            _event([_part("Checking… ")], author="agent", timestamp=2.0),
            _event(None, author="agent", timestamp=3.0),  # the tool call: no text
            _event([_part("all healthy.")], author="agent", timestamp=4.0),
        ]
    )
    assert [(t.role, t.text) for t in turns] == [
        ("user", "check kafka"),
        ("assistant", "Checking… all healthy."),
    ]
    # The merged message keeps the timestamp of where the answer started.
    assert turns[1].at == 2.0


def test_transcript_skips_tool_traffic_and_thoughts():
    turns = build_transcript(
        [
            _event([_part("do it")], author="user", timestamp=1.0),
            _event([], author="agent", timestamp=2.0),  # function_call event
            _event([_part("planning...", thought=True)], author="agent", timestamp=3.0),
            _event([_part("   ")], author="agent", timestamp=4.0),  # whitespace only
            _event([_part("Done.")], author="agent", timestamp=5.0),
        ]
    )
    assert [(t.role, t.text) for t in turns] == [("user", "do it"), ("assistant", "Done.")]


def test_transcript_skips_compaction_digests():
    """A compaction event is authored 'user' — replaying it would put an LLM
    summary of the conversation into the conversation as something the user said."""
    digest = _event([_part("Earlier: the operator asked about Kafka.")], author="user")
    digest.actions.compaction = MagicMock()
    turns = build_transcript([digest, _event([_part("hi")], author="user", timestamp=9.0)])
    assert [(t.role, t.text) for t in turns] == [("user", "hi")]


def test_transcript_of_empty_session_is_empty():
    assert build_transcript([]) == []
