"""``search_knowledge``: result shape, failure modes, and safety-chain wiring."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.retrieval.base_retrieval_tool import BaseRetrievalTool

from orrery_core.knowledge.config import KnowledgeConfig
from orrery_core.knowledge.factory import KnowledgeConfigError, knowledge_tool, resolve_retriever
from orrery_core.knowledge.models import Passage
from orrery_core.knowledge.tool import STALE_AFTER_DAYS, KnowledgeSearchTool

NOW = datetime.now(UTC)


class _FakeRetriever:
    def __init__(self, passages=None, error=None):
        self.passages = passages or []
        self.error = error
        self.calls: list[tuple[str, int]] = []

    async def retrieve(self, query, *, top_k, labels=None):
        self.calls.append((query, top_k))
        if self.error:
            raise self.error
        return self.passages


def _passage(*, age_days=1, uri="file://runbook.md", text="Restart the broker."):
    return Passage(
        text=text,
        uri=uri,
        title="Kafka ISR shrink",
        section="Recovery",
        revision="abc123",
        updated_at=NOW - timedelta(days=age_days),
        score=4.25,
    )


def _tool(retriever, **kw):
    return KnowledgeSearchTool(retriever, **kw)


def _ctx():
    return MagicMock()


def test_is_a_real_tool_so_the_plugin_chain_observes_it():
    # The entire safety argument for retrieval rests on this: a BaseTool result
    # passes through SafetyScreenPlugin (indirect injection), PIIRedaction,
    # the output cap and audit. ADK's VertexAiSearchTool is model built-in
    # grounding and would bypass all four.
    tool = _tool(_FakeRetriever())
    assert isinstance(tool, BaseRetrievalTool)
    assert isinstance(tool, BaseTool)


def test_declared_signature_is_just_query():
    declaration = _tool(_FakeRetriever())._get_declaration()
    params = declaration.parameters_json_schema or {}
    properties = params.get("properties") or declaration.parameters.properties
    assert set(properties) == {"query"}


@pytest.mark.asyncio
async def test_returns_passages_with_provenance():
    tool = _tool(_FakeRetriever([_passage()]))
    result = await tool.run_async(args={"query": "isr shrink"}, tool_context=_ctx())

    assert result["status"] == "success"
    hit = result["results"][0]
    # Provenance is required, not decorative: without it an operator cannot
    # tell a retrieved fact from a hallucination.
    assert hit["source"] == "file://runbook.md"
    assert hit["title"] == "Kafka ISR shrink"
    assert hit["section"] == "Recovery"
    assert hit["revision"] == "abc123"
    assert hit["age_days"] == 1
    assert hit["stale"] is False


@pytest.mark.asyncio
async def test_stale_documents_are_flagged_not_filtered():
    # A two-year-old runbook may be the only one there is — surface the age and
    # let the model discount it rather than hiding it.
    tool = _tool(_FakeRetriever([_passage(age_days=STALE_AFTER_DAYS + 10)]))
    result = await tool.run_async(args={"query": "isr"}, tool_context=_ctx())
    assert result["results"][0]["stale"] is True
    assert any("stale" in h or "not updated" in h for h in result["remediation_hints"])


@pytest.mark.asyncio
async def test_empty_corpus_is_success_not_error():
    # "Nothing is written down" is a fact about the corpus; reporting it as an
    # error would make the model think the search is broken.
    tool = _tool(_FakeRetriever([]))
    result = await tool.run_async(args={"query": "unknown thing"}, tool_context=_ctx())
    assert result["status"] == "success"
    assert result["results"] == []


@pytest.mark.asyncio
async def test_backend_failure_is_an_error_that_tells_the_model_not_to_conclude():
    tool = _tool(_FakeRetriever(error=RuntimeError("cluster down")))
    result = await tool.run_async(args={"query": "isr"}, tool_context=_ctx())
    assert result["status"] == "error"
    assert result["error_type"] == "KnowledgeBackendError"
    hints = " ".join(result["remediation_hints"])
    assert "do not treat this as" in hints.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "x", None, 123, "q" * 501])
async def test_invalid_queries_are_rejected_before_the_backend(bad):
    retriever = _FakeRetriever([_passage()])
    result = await _tool(retriever).run_async(args={"query": bad}, tool_context=_ctx())
    assert result["status"] == "error"
    assert retriever.calls == []


@pytest.mark.asyncio
async def test_top_k_is_passed_through_from_config():
    retriever = _FakeRetriever([])
    await _tool(retriever, top_k=3).run_async(args={"query": "isr"}, tool_context=_ctx())
    assert retriever.calls == [("isr", 3)]


@pytest.mark.asyncio
async def test_long_passages_are_clipped():
    # Hits re-enter the prompt on every later turn, so the budget bounds
    # ongoing cost, not just one response.
    tool = _tool(_FakeRetriever([_passage(text="word " * 5000)]))
    result = await tool.run_async(args={"query": "isr"}, tool_context=_ctx())
    text = result["results"][0]["text"]
    assert len(text) < 2000
    assert text.endswith("…[truncated]")


# ── Factory ──────────────────────────────────────────────────────────


def test_no_backend_configured_yields_no_tool():
    # An agent must not advertise a search tool with nothing behind it.
    assert knowledge_tool(KnowledgeConfig(orrery_knowledge_backend="none")) is None
    assert resolve_retriever(KnowledgeConfig(orrery_knowledge_backend="none")) is None


def test_elasticsearch_backend_builds_a_tool():
    tool = knowledge_tool(
        KnowledgeConfig(orrery_knowledge_backend="elasticsearch", orrery_knowledge_top_k=9)
    )
    assert isinstance(tool, KnowledgeSearchTool)
    assert tool._top_k == 9


def test_unknown_backend_fails_fast_at_startup():
    # A typo in a deployment manifest must not produce a pod that comes up
    # healthy while silently serving no corpus.
    with pytest.raises(KnowledgeConfigError, match="Unknown ORRERY_KNOWLEDGE_BACKEND"):
        knowledge_tool(KnowledgeConfig(orrery_knowledge_backend="pinecone"))
