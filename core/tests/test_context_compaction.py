"""Unit tests for conversation-history compaction (AEP-020).

Compaction is delegated to ADK's native ``EventsCompactionConfig``; what these
tests guard is our configuration of it — the on/off contract, the env-var
surface, and the two invariants the platform depends on: an explicit summarizer
is always attached, and every transport passes the config through to its ``App``.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.adk.apps.app import EventsCompactionConfig
from google.adk.apps.llm_event_summarizer import LlmEventSummarizer
from google.adk.models.google_llm import Gemini
from google.adk.models.lite_llm import LiteLlm

from orrery_core.agent.base import resolve_summarizer_model
from orrery_core.observability.metrics import CONTEXT_COMPACTION_TOTAL, track_compaction_event
from orrery_core.serving.runner import (
    DEFAULT_COMPACTION_TOKEN_THRESHOLD,
    _ObservedEventSummarizer,
    create_events_compaction_config,
)

# ── create_events_compaction_config ──────────────────────────────────


class TestCreateEventsCompactionConfig:
    def test_on_by_default(self):
        """Unset env means compaction is armed — the failure it prevents is silent."""
        config = create_events_compaction_config()
        assert config is not None
        assert config.token_threshold == DEFAULT_COMPACTION_TOKEN_THRESHOLD
        assert config.event_retention_size == 20
        assert config.compaction_interval == 50
        assert config.overlap_size == 2

    def test_default_threshold_is_out_of_reach_for_ordinary_sessions(self):
        """Enabling compaction must not change behaviour for normal traffic."""
        config = create_events_compaction_config()
        assert config is not None
        assert config.token_threshold is not None
        assert config.token_threshold >= 100_000

    @pytest.mark.parametrize("value", ["false", "0", "no", "off", "FALSE"])
    def test_master_switch_disables(self, value):
        with patch.dict("os.environ", {"ORRERY_CONTEXT_COMPACTION": value}):
            assert create_events_compaction_config() is None

    @pytest.mark.parametrize("value", ["true", "1", "yes", "on", "TRUE"])
    def test_master_switch_enables(self, value):
        with patch.dict("os.environ", {"ORRERY_CONTEXT_COMPACTION": value}):
            assert create_events_compaction_config() is not None

    def test_zero_threshold_disables(self):
        with patch.dict("os.environ", {"ORRERY_COMPACTION_TOKEN_THRESHOLD": "0"}):
            assert create_events_compaction_config() is None

    def test_negative_threshold_disables(self):
        assert create_events_compaction_config(token_threshold=-1) is None

    def test_explicit_overrides(self):
        config = create_events_compaction_config(
            token_threshold=1000,
            event_retention_size=4,
            compaction_interval=7,
            overlap_size=3,
        )
        assert config is not None
        assert config.token_threshold == 1000
        assert config.event_retention_size == 4
        assert config.compaction_interval == 7
        assert config.overlap_size == 3

    @patch.dict(
        "os.environ",
        {
            "ORRERY_COMPACTION_TOKEN_THRESHOLD": "5000",
            "ORRERY_COMPACTION_RETENTION_EVENTS": "8",
            "ORRERY_COMPACTION_INTERVAL": "12",
            "ORRERY_COMPACTION_OVERLAP": "1",
        },
    )
    def test_env_var_overrides(self):
        config = create_events_compaction_config()
        assert config is not None
        assert config.token_threshold == 5000
        assert config.event_retention_size == 8
        assert config.compaction_interval == 12
        assert config.overlap_size == 1

    @patch.dict("os.environ", {"ORRERY_COMPACTION_TOKEN_THRESHOLD": "5000"})
    def test_explicit_argument_beats_env_var(self):
        config = create_events_compaction_config(token_threshold=99)
        assert config is not None
        assert config.token_threshold == 99

    def test_summarizer_is_always_explicit(self):
        """Regression guard for the batch triage Workflow.

        Left to itself ADK derives the summarizer from the root agent's model and
        raises ``ValueError`` for a non-``LlmAgent`` root — which is exactly what
        ``orrery_triage_workflow`` is. It also bills summarization at the agent's
        own rate, which is the cost half of the same decision.
        """
        config = create_events_compaction_config()
        assert config is not None
        assert isinstance(config.summarizer, LlmEventSummarizer)

    def test_summarizer_is_instrumented(self):
        config = create_events_compaction_config()
        assert config is not None
        assert isinstance(config.summarizer, _ObservedEventSummarizer)


# ── resolve_summarizer_model ─────────────────────────────────────────


class TestResolveSummarizerModel:
    @patch.dict("os.environ", {"MODEL_PROVIDER": "gemini"}, clear=False)
    def test_gemini_defaults_to_a_cheap_model(self):
        with patch.dict("os.environ", {"ORRERY_COMPACTION_MODEL": ""}):
            model = resolve_summarizer_model()
        assert isinstance(model, Gemini)
        assert model.model == "gemini-flash-latest"

    @patch.dict(
        "os.environ",
        {"MODEL_PROVIDER": "gemini", "ORRERY_COMPACTION_MODEL": "gemini-2.0-flash"},
    )
    def test_gemini_honors_override(self):
        model = resolve_summarizer_model()
        assert isinstance(model, Gemini)
        assert model.model == "gemini-2.0-flash"

    @patch.dict(
        "os.environ",
        {
            "MODEL_PROVIDER": "anthropic",
            "ORRERY_COMPACTION_MODEL": "claude-haiku-4-5",
            "MODEL_NAME": "claude-opus-4",
        },
    )
    def test_non_gemini_prefixes_provider(self):
        model = resolve_summarizer_model()
        assert isinstance(model, LiteLlm)
        assert model.model == "anthropic/claude-haiku-4-5"

    @patch.dict(
        "os.environ",
        {
            "MODEL_PROVIDER": "anthropic",
            "ORRERY_COMPACTION_MODEL": "anthropic/claude-haiku-4-5",
        },
    )
    def test_non_gemini_leaves_qualified_name_alone(self):
        model = resolve_summarizer_model()
        assert isinstance(model, LiteLlm)
        assert model.model == "anthropic/claude-haiku-4-5"

    @patch.dict(
        "os.environ",
        {"MODEL_PROVIDER": "openai", "ORRERY_COMPACTION_MODEL": "", "MODEL_NAME": "gpt-4o-mini"},
    )
    def test_non_gemini_falls_back_to_agent_model(self):
        """No cross-provider cheap default exists, so reuse the agent's model."""
        model = resolve_summarizer_model()
        assert isinstance(model, LiteLlm)
        assert model.model == "openai/gpt-4o-mini"

    @patch.dict(
        "os.environ",
        {"MODEL_PROVIDER": "openai", "ORRERY_COMPACTION_MODEL": "", "MODEL_NAME": ""},
    )
    def test_non_gemini_without_any_model_raises(self):
        with pytest.raises(ValueError, match="ORRERY_COMPACTION_MODEL or MODEL_NAME"):
            resolve_summarizer_model()


# ── _ObservedEventSummarizer ─────────────────────────────────────────


class TestObservedEventSummarizer:
    """The summarizer is our only observation point.

    ADK appends the compaction event straight to the session service once the
    agent's generator is exhausted, so it never reaches ``on_event_callback``.
    """

    @pytest.mark.asyncio
    async def test_counts_a_successful_compaction(self):
        summarizer = _ObservedEventSummarizer(llm=MagicMock())
        before = CONTEXT_COMPACTION_TOTAL._value.get()

        with patch.object(
            LlmEventSummarizer,
            "maybe_summarize_events",
            new=AsyncMock(return_value=MagicMock()),
        ):
            result = await summarizer.maybe_summarize_events(events=[MagicMock()])

        assert result is not None
        assert CONTEXT_COMPACTION_TOTAL._value.get() == before + 1

    @pytest.mark.asyncio
    async def test_does_not_count_when_nothing_was_compacted(self):
        summarizer = _ObservedEventSummarizer(llm=MagicMock())
        before = CONTEXT_COMPACTION_TOTAL._value.get()

        with patch.object(
            LlmEventSummarizer,
            "maybe_summarize_events",
            new=AsyncMock(return_value=None),
        ):
            result = await summarizer.maybe_summarize_events(events=[MagicMock()])

        assert result is None
        assert CONTEXT_COMPACTION_TOTAL._value.get() == before


def test_track_compaction_event_increments():
    before = CONTEXT_COMPACTION_TOTAL._value.get()
    track_compaction_event()
    assert CONTEXT_COMPACTION_TOTAL._value.get() == before + 1


# ── Wiring: the config reaches the App ───────────────────────────────


class TestGatewayWiring:
    """A config nothing forwards is a config that does nothing."""

    def _app_kwargs(self, **kwargs):
        """Build a gateway with ADK stubbed out, returning the ``App`` kwargs."""
        from orrery_core.serving import gateway as gateway_mod

        with (
            patch.object(gateway_mod, "App") as app_cls,
            patch.object(gateway_mod, "Runner"),
            patch.object(gateway_mod, "create_session_service", return_value=MagicMock()),
        ):
            gateway_mod.AgentGateway(app_name="t", root_agent=MagicMock(), **kwargs)
        return app_cls.call_args.kwargs

    def test_gateway_forwards_config_to_app(self):
        config = create_events_compaction_config(token_threshold=123)
        assert self._app_kwargs(events_compaction_config=config)["events_compaction_config"] is (
            config
        )

    def test_gateway_without_config_leaves_compaction_off(self):
        assert self._app_kwargs()["events_compaction_config"] is None

    def test_server_defaults_compaction_on(self):
        """``create_app`` resolves the factory itself, so the HTTP front door is covered."""
        from orrery_core.serving import server

        with patch.object(server, "AgentGateway") as gateway_cls:
            server.create_app(
                root_agent=MagicMock(),
                app_name="t",
                config=server.ServerConfig(auth_enabled=False),
            )

        passed = gateway_cls.call_args.kwargs["events_compaction_config"]
        assert passed is not None
        assert passed.token_threshold == DEFAULT_COMPACTION_TOKEN_THRESHOLD

    def test_server_respects_the_master_switch(self):
        from orrery_core.serving import server

        with (
            patch.dict("os.environ", {"ORRERY_CONTEXT_COMPACTION": "false"}),
            patch.object(server, "AgentGateway") as gateway_cls,
        ):
            server.create_app(
                root_agent=MagicMock(),
                app_name="t",
                config=server.ServerConfig(auth_enabled=False),
            )

        assert gateway_cls.call_args.kwargs["events_compaction_config"] is None


class TestWorkflowRootContract:
    """`orrery_triage_workflow` is a Workflow, not an LlmAgent.

    ADK derives the summarizer from the root agent's model when none is given,
    and raises for a non-``LlmAgent`` root — so the batch triage entrypoint only
    works because the factory always supplies one.
    """

    def _non_llm_root(self):
        from google.adk.agents.base_agent import BaseAgent

        class NotAnLlmAgent(BaseAgent):
            pass

        return NotAnLlmAgent(name="workflow_like")

    def test_our_config_survives_a_non_llm_root(self):
        from google.adk.apps.compaction import _ensure_compaction_summarizer

        config = create_events_compaction_config()
        assert config is not None
        _ensure_compaction_summarizer(config=config, agent=self._non_llm_root())

    def test_a_bare_config_would_crash_the_batch_root(self):
        """Pins the ADK behaviour the test above is protecting against."""
        from google.adk.apps.compaction import _ensure_compaction_summarizer

        with pytest.raises(ValueError, match="No LlmAgent model available"):
            _ensure_compaction_summarizer(
                config=EventsCompactionConfig(compaction_interval=5, overlap_size=1),
                agent=self._non_llm_root(),
            )


class TestConfigContract:
    def test_adk_requires_threshold_and_retention_together(self):
        """Documents the ADK validator our factory has to satisfy."""
        with pytest.raises(ValueError, match="must be set together"):
            EventsCompactionConfig(
                compaction_interval=5,
                overlap_size=1,
                token_threshold=1000,
            )
