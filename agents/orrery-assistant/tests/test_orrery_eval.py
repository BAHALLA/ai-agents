"""Agent-level evaluation for the planner-enabled orrery-assistant root.

Runs ADK's ``AgentEvaluator`` against the routing scenarios in
``tests/evals/``. The point of these scenarios is *not* to exercise every
tool — it is to verify that **with a planner attached** the root
orchestrator routes narrow queries to the correct specialist AgentTool
instead of falling through to the comprehensive ``incident_triage_agent``.

Like the per-specialist eval suites, this test:

* Uses a real LLM via the agent's own ``.env`` (Vertex / AI Studio).
* Is gated behind the ``eval`` pytest marker (``make eval``).
* Skips automatically when no Gemini credentials are configured.
* Mocks the underlying client-getters so no real Kafka or Elasticsearch
  is required — we only need the LLM's tool selection, not realistic
  data.
"""

import os
from unittest.mock import MagicMock, patch

import pytest
from google.adk.evaluation.agent_evaluator import AgentEvaluator

import elasticsearch_agent.tools as _es_tools
import kafka_health_agent.tools as _kafka_tools
from orrery_core import load_agent_env

EVAL_DIR = os.path.join(os.path.dirname(__file__), "evals")

# Load orrery-assistant's .env so the eval uses the same model + auth as
# the agent itself. Done at import time so the skip guard below sees the
# loaded values.
import orrery_assistant.agent as _agent_mod  # noqa: E402

load_agent_env(_agent_mod.__file__)


def _has_llm_credentials() -> bool:
    """Same shape as the per-specialist eval guards."""
    if os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"):
        return True
    if os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
        return True
    return bool(
        os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").upper() == "TRUE"
        and os.getenv("GOOGLE_CLOUD_PROJECT")
    )


def _make_kafka_admin_client() -> MagicMock:
    """Minimal Kafka AdminClient mock — enough for get_kafka_cluster_health
    to return a coherent ``healthy`` response when the kafka_health_agent
    AgentTool is invoked by the orchestrator."""
    admin = MagicMock()
    metadata = MagicMock()
    broker1 = MagicMock(id=1, host="broker-1", port=9092)
    broker2 = MagicMock(id=2, host="broker-2", port=9092)
    metadata.brokers = {1: broker1, 2: broker2}
    metadata.topics = {}  # empty is fine; the agent reports broker count
    admin.list_topics.return_value = metadata
    return admin


def _make_es_session() -> MagicMock:
    """Minimal Elasticsearch ``requests.Session`` mock returning a green
    cluster health body. The session is reached through a getter, so a
    single MagicMock with a ``.get`` return value is enough."""
    session = MagicMock()
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "cluster_name": "orrery-test",
        "status": "green",
        "number_of_nodes": 1,
        "active_primary_shards": 0,
        "active_shards": 0,
        "unassigned_shards": 0,
    }
    response.raise_for_status = MagicMock()
    session.get.return_value = response
    return session


@pytest.fixture(autouse=True)
def _reset_module_singletons():
    """Reset cached clients / sessions between tests so each scenario gets
    its own mock-driven view of the world."""
    _kafka_tools._admin_client = None
    _es_tools._session = None
    yield
    _kafka_tools._admin_client = None
    _es_tools._session = None


@pytest.mark.eval
@pytest.mark.asyncio
async def test_planner_routing_eval(monkeypatch):
    """Verify ORRERY_PLANNER=plan_react routes narrow queries to the
    right specialist AgentTool. Mocks both kafka admin and the ES
    session at the same client-getter layer the unit tests use.

    Thresholds in ``evals/test_config.json`` are set to 1.0 for the
    custom ``tool_trajectory_name_match`` metric. The expected ``tool_uses``
    in the JSON specifies only the AgentTool name; the actual call carries
    an LLM-generated ``request`` arg whose text is non-deterministic.
    Our custom metric ignores the exact arguments and strictly matches
    the tool trajectory names."""
    if not _has_llm_credentials():
        pytest.skip(
            "Agent eval requires Gemini credentials: set GOOGLE_API_KEY / "
            "GEMINI_API_KEY, or configure Vertex AI via "
            "GOOGLE_GENAI_USE_VERTEXAI=TRUE + GOOGLE_CLOUD_PROJECT in the "
            "agent's .env."
        )

    # Force the planner on for this eval run, regardless of what the
    # developer's .env has set.
    monkeypatch.setenv("ORRERY_PLANNER", "plan_react")

    # Reload the agent module so resolve_planner() picks up the new value
    # at import time (the module attaches the planner during import).
    import importlib

    importlib.reload(_agent_mod)

    with (
        patch(
            "kafka_health_agent.tools._get_admin_client",
            return_value=_make_kafka_admin_client(),
        ),
        patch(
            "elasticsearch_agent.tools._get_session",
            return_value=_make_es_session(),
        ),
    ):
        await AgentEvaluator.evaluate(
            agent_module="orrery_assistant.agent",
            eval_dataset_file_path_or_dir=EVAL_DIR,
            num_runs=1,
        )
