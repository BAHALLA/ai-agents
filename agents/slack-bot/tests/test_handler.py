"""Tests for the Slack → ADK message handler."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orrery_core import AgentGateway
from slack_bot.handler import APP_NAME, SlackAgentHandler


@pytest.fixture
def mock_session_service():
    service = AsyncMock()
    session = MagicMock()
    session.id = "sess_new"
    service.create_session = AsyncMock(return_value=session)
    return service


@pytest.fixture
def mock_runner():
    return AsyncMock()


@pytest.fixture
def gateway(mock_runner, mock_session_service):
    """A real AgentGateway wrapping the mock runner + session service."""
    with patch("orrery_core.serving.gateway.App"), patch("orrery_core.serving.gateway.Runner"):
        gw = AgentGateway(
            app_name=APP_NAME,
            root_agent=MagicMock(),
            plugins=[],
            session_service=mock_session_service,
        )
    gw.runner = mock_runner
    return gw


@pytest.fixture
def handler(gateway, session_map, channel_ref):
    return SlackAgentHandler(
        gateway=gateway,
        session_map=session_map,
        channel_ref=channel_ref,
    )


def _make_event(text_content: str):
    """Create a fake ADK event with text parts."""
    part = MagicMock()
    part.text = text_content
    part.thought = None
    content = MagicMock()
    content.parts = [part]
    event = MagicMock()
    event.content = content
    return event


def _make_thought_event(thought_text: str, answer_text: str):
    """Fake ADK event with a Gemini thought part plus the real answer."""
    thought = MagicMock()
    thought.text = thought_text
    thought.thought = True
    answer = MagicMock()
    answer.text = answer_text
    answer.thought = None
    content = MagicMock()
    content.parts = [thought, answer]
    event = MagicMock()
    event.content = content
    return event


async def _empty_run_async(**kwargs):
    """Async generator that yields nothing — used as a mock for runner.run_async."""
    return
    yield  # noqa: E501 — makes this an async generator


class TestSlackAgentHandler:
    @pytest.mark.asyncio
    async def test_creates_new_session_for_new_thread(
        self, handler, mock_runner, mock_session_service, session_map, say
    ):
        mock_runner.run_async = _empty_run_async

        await handler.handle_message(
            text="check kafka",
            channel="C_CHAN",
            thread_ts="111.000",
            user_id="U_USER",
            say=say,
        )

        mock_session_service.create_session.assert_called_once()
        assert session_map.get("C_CHAN", "111.000") == "sess_new"

    @pytest.mark.asyncio
    async def test_reuses_existing_session(
        self, handler, mock_runner, mock_session_service, session_map, say
    ):
        session_map.set("C_CHAN", "111.000", "sess_existing")
        mock_runner.run_async = _empty_run_async

        await handler.handle_message(
            text="check kafka",
            channel="C_CHAN",
            thread_ts="111.000",
            user_id="U_USER",
            say=say,
        )

        mock_session_service.create_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_posts_response_to_thread(self, handler, mock_runner, say):
        event = _make_event("Kafka is healthy.")

        async def fake_run_async(**kwargs):
            yield event

        mock_runner.run_async = fake_run_async

        await handler.handle_message(
            text="check kafka",
            channel="C_CHAN",
            thread_ts="111.000",
            user_id="U_USER",
            say=say,
        )

        say.assert_called()
        call_kwargs = say.call_args
        assert "Kafka is healthy" in call_kwargs.kwargs.get(
            "text", call_kwargs.args[0] if call_kwargs.args else ""
        )

    @pytest.mark.asyncio
    async def test_thinking_parts_excluded_from_reply(self, handler, mock_runner, say):
        """Gemini planner thought parts must not leak into the Slack reply."""
        event = _make_thought_event("Let me reason about the cluster...", "Kafka is healthy.")

        async def fake_run_async(**kwargs):
            yield event

        mock_runner.run_async = fake_run_async

        await handler.handle_message(
            text="check kafka",
            channel="C_CHAN",
            thread_ts="111.000",
            user_id="U_USER",
            say=say,
        )

        say.assert_called()
        call_kwargs = say.call_args
        posted = call_kwargs.kwargs.get("text", call_kwargs.args[0] if call_kwargs.args else "")
        assert "Kafka is healthy" in posted
        assert "reason about" not in posted

    @pytest.mark.asyncio
    async def test_empty_response_not_posted(self, handler, mock_runner, say):
        # Event with no text
        event = MagicMock()
        event.content = None

        async def fake_run_async(**kwargs):
            yield event

        mock_runner.run_async = fake_run_async

        await handler.handle_message(
            text="hello",
            channel="C_CHAN",
            thread_ts="111.000",
            user_id="U_USER",
            say=say,
        )

        say.assert_not_called()

    @pytest.mark.asyncio
    async def test_runner_error_posts_error_message(self, handler, mock_runner, say):
        async def failing_run(**kwargs):
            raise RuntimeError("LLM failed")
            yield  # make it an async generator  # noqa: E501

        mock_runner.run_async = failing_run

        await handler.handle_message(
            text="check kafka",
            channel="C_CHAN",
            thread_ts="111.000",
            user_id="U_USER",
            say=say,
        )

        say.assert_called_once()
        assert (
            "wrong"
            in say.call_args.kwargs.get(
                "text", say.call_args.args[0] if say.call_args.args else ""
            ).lower()
        )

    @pytest.mark.asyncio
    async def test_updates_channel_ref(self, handler, mock_runner, channel_ref, say):
        mock_runner.run_async = _empty_run_async

        await handler.handle_message(
            text="hello",
            channel="C_NEW",
            thread_ts="999.000",
            user_id="U_USER",
            say=say,
        )

        assert channel_ref["channel"] == "C_NEW"
        assert channel_ref["thread_ts"] == "999.000"
