"""Elasticsearch backend: request shape, error handling, protocol conformance.

Mocks the HTTP layer rather than requiring a running cluster, matching the rest
of the suite. What is asserted is the contract the seam promises — including
the two failures that are easy to get wrong: ``_bulk`` reporting per-item
errors under a 200, and "unreachable" having to be distinguishable from "no
matches".
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
import requests

from orrery_core.knowledge.backends.elasticsearch import (
    ElasticsearchKnowledgeBackend,
    KnowledgeBackendError,
)
from orrery_core.knowledge.config import KnowledgeConfig
from orrery_core.knowledge.models import Chunk
from orrery_core.knowledge.protocols import KnowledgeIndex, KnowledgeRetriever

NOW = datetime(2026, 8, 1, tzinfo=UTC)


@pytest.fixture
def backend():
    return ElasticsearchKnowledgeBackend(
        KnowledgeConfig(
            knowledge_es_url="http://es.test:9200",
            knowledge_es_index="test-knowledge",
        )
    )


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.content = b"x" if payload is not None or text else b""

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _chunk(ordinal=0, revision="rev1"):
    return Chunk(
        uri="file://runbook.md",
        title="Kafka ISR shrink",
        section="Recovery",
        text="Restart the broker.",
        revision=revision,
        updated_at=NOW,
        ordinal=ordinal,
        labels={"collection": "runbooks"},
    )


def test_backend_satisfies_both_protocols(backend):
    # A self-hosted backend owns both halves; a managed one would implement
    # only the retriever, and sync would skip it.
    assert isinstance(backend, KnowledgeRetriever)
    assert isinstance(backend, KnowledgeIndex)


@pytest.mark.asyncio
async def test_retrieve_builds_a_weighted_query_and_maps_provenance(backend):
    payload = {
        "hits": {
            "hits": [
                {
                    "_score": 4.5,
                    "_source": {
                        "uri": "file://runbook.md",
                        "title": "Kafka ISR shrink",
                        "section": "Recovery",
                        "text": "Restart the broker.",
                        "revision": "abc123",
                        "updated_at": "2026-07-01T00:00:00+00:00",
                    },
                }
            ]
        }
    }
    with patch.object(requests.Session, "request", return_value=_Response(payload=payload)) as m:
        passages = await backend.retrieve("isr shrink", top_k=3)

    body = m.call_args.kwargs["json"]
    # Title and section outrank body text: a runbook named for the symptom
    # should beat a postmortem that mentions it in passing.
    assert body["query"]["multi_match"]["fields"] == ["title^3", "section^2", "text"]
    assert body["size"] == 3
    # A server-side timeout matters independently of our HTTP timeout: when
    # ours fires the query keeps burning cluster resources.
    assert body["timeout"] == backend._config.knowledge_es_search_timeout

    assert len(passages) == 1
    passage = passages[0]
    assert passage.uri == "file://runbook.md"
    assert passage.section == "Recovery"
    assert passage.revision == "abc123"
    assert passage.score == 4.5


@pytest.mark.asyncio
async def test_label_filters_are_exact_match_terms(backend):
    with patch.object(
        requests.Session, "request", return_value=_Response(payload={"hits": {"hits": []}})
    ) as m:
        await backend.retrieve("q", top_k=2, labels={"collection": "runbooks"})
    body = m.call_args.kwargs["json"]
    assert body["query"]["bool"]["filter"] == [{"term": {"labels.collection": "runbooks"}}]


@pytest.mark.asyncio
async def test_no_matches_returns_empty_not_an_error(backend):
    with patch.object(
        requests.Session, "request", return_value=_Response(payload={"hits": {"hits": []}})
    ):
        assert await backend.retrieve("nothing", top_k=5) == []


@pytest.mark.asyncio
async def test_unreachable_cluster_raises_rather_than_returning_empty(backend):
    # The distinction is load-bearing: a down cluster that looked like "no
    # matches" would silently become "we have no runbook for that".
    with (
        patch.object(requests.Session, "request", side_effect=requests.ConnectionError("refused")),
        pytest.raises(KnowledgeBackendError, match="Cannot reach"),
    ):
        await backend.retrieve("q", top_k=5)


@pytest.mark.asyncio
async def test_http_error_raises(backend):
    with (
        patch.object(
            requests.Session, "request", return_value=_Response(status_code=500, text="boom")
        ),
        pytest.raises(KnowledgeBackendError, match="HTTP 500"),
    ):
        await backend.retrieve("q", top_k=5)


@pytest.mark.asyncio
async def test_upsert_sends_ndjson_keyed_by_deterministic_id(backend):
    chunk = _chunk()
    with patch.object(
        requests.Session, "request", return_value=_Response(payload={"errors": False})
    ) as m:
        await backend.upsert([chunk])
    kwargs = m.call_args.kwargs
    assert kwargs["headers"]["Content-Type"] == "application/x-ndjson"
    lines = kwargs["data"].strip().split("\n")
    assert len(lines) == 2
    assert chunk.id in lines[0]
    assert "test-knowledge" in lines[0]
    assert "Restart the broker." in lines[1]


@pytest.mark.asyncio
async def test_bulk_partial_failure_is_surfaced(backend):
    # _bulk answers 200 even when individual documents fail; without checking
    # per-item errors a half-indexed corpus looks like a clean sync.
    payload = {
        "errors": True,
        "items": [{"index": {"error": {"type": "mapper_parsing_exception"}}}],
    }
    with (
        patch.object(requests.Session, "request", return_value=_Response(payload=payload)),
        pytest.raises(KnowledgeBackendError, match="failed to index"),
    ):
        await backend.upsert([_chunk()])


@pytest.mark.asyncio
async def test_upsert_of_nothing_makes_no_request(backend):
    with patch.object(requests.Session, "request") as m:
        await backend.upsert([])
    m.assert_not_called()


@pytest.mark.asyncio
async def test_delete_stale_targets_other_revisions_of_the_same_uri(backend):
    with patch.object(
        requests.Session, "request", return_value=_Response(payload={"deleted": 5})
    ) as m:
        deleted = await backend.delete_stale("file://runbook.md", "rev2")
    assert deleted == 5
    query = m.call_args.kwargs["json"]["query"]["bool"]
    assert query["must"] == [{"term": {"uri": "file://runbook.md"}}]
    assert query["must_not"] == [{"term": {"revision": "rev2"}}]


@pytest.mark.asyncio
async def test_ensure_ready_creates_the_index_only_when_missing(backend):
    responses = [_Response(status_code=404, text="not found"), _Response(payload={"ok": True})]
    with patch.object(requests.Session, "request", side_effect=responses) as m:
        await backend.ensure_ready()
    assert m.call_args_list[0].args[0] == "HEAD"
    assert m.call_args_list[1].args[0] == "PUT"

    with patch.object(requests.Session, "request", return_value=_Response(payload={})) as m:
        await backend.ensure_ready()
    assert m.call_count == 1  # HEAD succeeded; no PUT


@pytest.mark.asyncio
async def test_indexed_revisions_flattens_the_aggregation(backend):
    payload = {
        "aggregations": {
            "by_uri": {
                "buckets": [
                    {"key": "file://a.md", "rev": {"buckets": [{"key": "sha-a"}]}},
                    {"key": "file://b.md", "rev": {"buckets": []}},
                ]
            }
        }
    }
    with patch.object(requests.Session, "request", return_value=_Response(payload=payload)):
        assert await backend.indexed_revisions() == {"file://a.md": "sha-a"}


@pytest.mark.asyncio
async def test_api_key_and_basic_auth_are_mutually_exclusive():
    keyed = ElasticsearchKnowledgeBackend(
        KnowledgeConfig(knowledge_es_api_key="k", knowledge_es_username="u")
    )
    session = keyed._get_session()
    assert session.headers["Authorization"] == "ApiKey k"
    assert session.auth is None

    basic = ElasticsearchKnowledgeBackend(
        KnowledgeConfig(knowledge_es_username="u", knowledge_es_password="p")
    )
    assert basic._get_session().auth == ("u", "p")
