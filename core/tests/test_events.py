"""Tests for orrery_core.serving.events.extract_reply_text."""

from __future__ import annotations

from unittest.mock import MagicMock

from orrery_core import extract_reply_text


def _part(text: str | None, *, thought: bool | None = None) -> MagicMock:
    part = MagicMock()
    part.text = text
    part.thought = thought
    return part


def _event(parts: list | None) -> MagicMock:
    event = MagicMock()
    if parts is None:
        event.content = None
    else:
        content = MagicMock()
        content.parts = parts
        event.content = content
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
