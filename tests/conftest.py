"""Shared test fixtures for mocking ADK objects."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock


class FakeState(dict):
    """A plain dict that behaves like ADK's State object."""
    pass


class FakeTool:
    """Minimal mock of ADK's BaseTool."""

    def __init__(self, name: str, func: Any = None):
        self.name = name
        self.func = func


class FakeToolContext:
    """Minimal mock of ADK's Context / ToolContext."""

    def __init__(self, state: dict | None = None):
        self.state = FakeState(state or {})
        self.agent_name = "test_agent"
        self.user_id = "test_user"
        self.session = MagicMock()
        self.session.id = "test_session_123"
