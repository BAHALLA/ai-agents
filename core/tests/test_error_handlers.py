"""Unit tests for error handler callback factories."""

from unittest.mock import MagicMock

from orrery_core.reliability.error_handlers import graceful_model_error, graceful_tool_error

# ── graceful_tool_error ───────────────────────────────────────────────


def test_graceful_tool_error_returns_dict():
    callback = graceful_tool_error()
    tool = MagicMock()
    tool.name = "get_kafka_cluster_health"

    result = callback(tool, {"timeout": 10}, MagicMock(), ConnectionError("broker down"))

    assert result["status"] == "error"
    assert result["error_type"] == "ConnectionError"
    assert "get_kafka_cluster_health" in result["message"]


def test_graceful_tool_error_keeps_raw_exception_text_out_of_the_model_context():
    """Client exception strings embed internal hosts, URLs and paths (CWE-209).

    They belong in the server log, not in the transcript the model reads from
    and quotes back to the user.
    """
    callback = graceful_tool_error()
    tool = MagicMock()
    tool.name = "get_pod_logs"

    leaky = ConnectionError(
        "HTTPSConnectionPool(host='10.0.4.17', port=6443): /var/run/secrets/token"
    )
    result = callback(tool, {}, MagicMock(), leaky)

    assert "10.0.4.17" not in result["message"]
    assert "/var/run/secrets" not in result["message"]
    assert result["error_type"] == "ConnectionError"


def test_graceful_tool_error_surfaces_4xx_detail_so_the_model_can_correct():
    callback = graceful_tool_error()
    tool = MagicMock()
    tool.name = "scale_deployment"

    class ApiError(Exception):
        status_code = 422
        detail = "replicas must be >= 0"

    result = callback(tool, {"replicas": -1}, MagicMock(), ApiError())

    assert "422" in result["message"]
    assert "replicas must be >= 0" in result["message"]


def test_graceful_tool_error_hides_5xx_detail():
    callback = graceful_tool_error()
    tool = MagicMock()
    tool.name = "list_pods"

    class ApiError(Exception):
        status_code = 503
        detail = "upstream backend 10.0.0.9 unavailable"

    result = callback(tool, {}, MagicMock(), ApiError())
    assert "10.0.0.9" not in result["message"]


def test_graceful_tool_error_handles_generic_exception():
    callback = graceful_tool_error()
    tool = MagicMock()
    tool.name = "list_pods"

    result = callback(tool, {}, MagicMock(), RuntimeError("unexpected"))

    assert result["status"] == "error"
    assert result["error_type"] == "RuntimeError"


def test_graceful_tool_error_always_returns_dict():
    """Callback should always return a dict (never None) so the LLM can reason about it."""
    callback = graceful_tool_error()
    tool = MagicMock()
    tool.name = "test_tool"

    result = callback(tool, {}, MagicMock(), Exception(""))
    assert isinstance(result, dict)


# ── graceful_model_error ──────────────────────────────────────────────


def test_graceful_model_error_returns_llm_response():
    callback = graceful_model_error()
    result = callback(MagicMock(), MagicMock(), TimeoutError("model timed out"))

    assert result.content is not None
    assert len(result.content.parts) == 1
    assert result.content.role == "model"
    assert "TimeoutError" in result.content.parts[0].text


def test_graceful_model_error_reads_as_a_failure_not_an_answer():
    """A specialist's model failure becomes that AgentTool's *result* in the
    coordinator's transcript. It must be unmistakably a failure, or the
    coordinator treats the step as done and reports success for work that
    never ran."""
    callback = graceful_model_error()
    text = callback(MagicMock(), MagicMock(), RuntimeError("boom")).content.parts[0].text

    assert "STEP FAILED" in text
    assert "No result was produced" in text
    assert "do not continue" in text.lower()


def test_graceful_model_error_keeps_raw_error_text_private():
    callback = graceful_model_error()
    leaky = RuntimeError("POST https://internal-vertex.corp/v1/projects/secret-proj:generate")
    text = callback(MagicMock(), MagicMock(), leaky).content.parts[0].text

    assert "internal-vertex.corp" not in text
    assert "RuntimeError" in text


def test_graceful_model_error_names_quota_exhaustion():
    class ResourceExhaustedError(Exception):
        pass

    callback = graceful_model_error()
    text = callback(MagicMock(), MagicMock(), ResourceExhaustedError()).content.parts[0].text
    assert "429" in text


def test_graceful_model_error_finds_quota_status_on_the_cause_chain():
    """ADK wraps the provider error, so the status lives on the cause."""

    class ProviderError(Exception):
        status_code = 429

    wrapped = RuntimeError("model call failed")
    wrapped.__cause__ = ProviderError()

    callback = graceful_model_error()
    text = callback(MagicMock(), MagicMock(), wrapped).content.parts[0].text
    assert "429" in text


def test_graceful_model_error_handles_generic_exception():
    callback = graceful_model_error()
    result = callback(MagicMock(), MagicMock(), RuntimeError("quota exceeded"))

    assert "RuntimeError" in result.content.parts[0].text
