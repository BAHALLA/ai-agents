"""AEP-025 phase 2: embeddings, pgvector, Confluence.

The pgvector backend's SQL is exercised against a real PostgreSQL in
``test_knowledge_pgvector_integration`` (skipped without one); what is unit
tested here is everything that can be wrong without a database — provider
selection, the asymmetric embed contract, statement shape, and the Confluence
source's parsing and its refusal to auto-discover spaces.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from orrery_core.knowledge.config import KnowledgeConfig
from orrery_core.knowledge.embedding import (
    DEFAULT_MODELS,
    EmbeddingError,
    GeminiEmbedder,
    LiteLlmEmbedder,
    resolve_embedder,
)
from orrery_core.knowledge.factory import resolve_index, resolve_retriever
from orrery_core.knowledge.protocols import KnowledgeIndex, KnowledgeRetriever
from orrery_core.knowledge.sources.confluence import (
    ConfluenceSource,
    build_from_env,
    html_to_text,
)

# ── Embedder resolution ──────────────────────────────────────────────


class TestResolveEmbedder:
    def test_defaults_to_the_chat_provider(self, monkeypatch):
        # One fewer variable to set, and almost always what a deployment wants.
        monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
        monkeypatch.setenv("MODEL_PROVIDER", "openai")
        embedder = resolve_embedder()
        assert isinstance(embedder, LiteLlmEmbedder)
        assert embedder.dimensions == DEFAULT_MODELS["openai"][1]

    def test_explicit_embedding_provider_wins(self, monkeypatch):
        monkeypatch.setenv("MODEL_PROVIDER", "openai")
        monkeypatch.setenv("EMBEDDING_PROVIDER", "gemini")
        assert isinstance(resolve_embedder(), GeminiEmbedder)

    def test_non_gemini_providers_go_through_litellm(self, monkeypatch):
        # The whole point of the seam: a GCP-only embedder would make the
        # Ollama and Claude deployments second-class.
        monkeypatch.setenv("EMBEDDING_PROVIDER", "ollama")
        embedder = resolve_embedder()
        assert isinstance(embedder, LiteLlmEmbedder)
        assert embedder.model.startswith("ollama/")  # ty: narrowed by isinstance

    def test_bare_model_names_are_provider_prefixed(self, monkeypatch):
        # LiteLLM cannot route a bare name, and the failure it produces is a
        # confusing routing error rather than a clear one.
        monkeypatch.setenv("EMBEDDING_PROVIDER", "ollama")
        monkeypatch.setenv("EMBEDDING_MODEL", "nomic-embed-text")
        embedder = resolve_embedder()
        assert isinstance(embedder, LiteLlmEmbedder)
        assert embedder.model == "ollama/nomic-embed-text"

    def test_already_prefixed_models_are_left_alone(self, monkeypatch):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
        monkeypatch.setenv("EMBEDDING_MODEL", "openai/text-embedding-3-large")
        embedder = resolve_embedder()
        assert isinstance(embedder, LiteLlmEmbedder)
        assert embedder.model == "openai/text-embedding-3-large"

    def test_dimensions_are_overridable(self, monkeypatch):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
        monkeypatch.setenv("EMBEDDING_DIMENSIONS", "256")
        assert resolve_embedder().dimensions == 256


class TestEmbedContract:
    @pytest.mark.asyncio
    async def test_empty_batch_makes_no_call(self):
        with patch("litellm.aembedding") as m:
            assert await LiteLlmEmbedder("openai/x", 8).embed([], kind="document") == []
        m.assert_not_called()

    @pytest.mark.asyncio
    async def test_results_are_reordered_by_index(self):
        # LiteLLM normalises to the OpenAI shape but does not promise ordering,
        # and a silently transposed batch would mis-attribute every vector to
        # the wrong chunk — invisible except as bad retrieval.
        response = {"data": [{"index": 1, "embedding": [9.0]}, {"index": 0, "embedding": [1.0]}]}
        with patch("litellm.aembedding", return_value=response):
            vectors = await LiteLlmEmbedder("openai/x", 1).embed(["a", "b"], kind="document")
        assert vectors == [[1.0], [9.0]]

    @pytest.mark.asyncio
    async def test_provider_failure_becomes_a_typed_error(self):
        with (
            patch("litellm.aembedding", side_effect=RuntimeError("quota")),
            pytest.raises(EmbeddingError, match="Embedding failed"),
        ):
            await LiteLlmEmbedder("openai/x", 8).embed(["a"], kind="query")

    @pytest.mark.asyncio
    async def test_gemini_passes_the_retrieval_task_type(self):
        # Retrieval is asymmetric: a question and the passage answering it are
        # not paraphrases, and Gemini produces better neighbours when told
        # which side it is embedding.
        embedder = GeminiEmbedder("gemini-embedding-001", 4)
        client = MagicMock()
        seen: list[dict] = []

        async def _embed(**kwargs):
            seen.append(kwargs)
            return MagicMock(embeddings=[MagicMock(values=[0.1, 0.2, 0.3, 0.4])])

        client.aio.models.embed_content = _embed
        embedder._client = client

        await embedder.embed(["q"], kind="query")
        assert seen[-1]["config"].task_type == "RETRIEVAL_QUERY"
        await embedder.embed(["d"], kind="document")
        assert seen[-1]["config"].task_type == "RETRIEVAL_DOCUMENT"


# ── pgvector backend (no database needed) ────────────────────────────


class _FakeEmbedder:
    dimensions = 4

    def __init__(self):
        self.calls: list[tuple[int, str]] = []

    async def embed(self, texts, *, kind):
        self.calls.append((len(texts), kind))
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


def _pg_backend(embedder=None):
    from orrery_core.knowledge.backends.pgvector import PgVectorKnowledgeBackend

    return PgVectorKnowledgeBackend(
        KnowledgeConfig(
            orrery_knowledge_backend="pgvector",
            knowledge_pg_url="postgresql://user:pw@db:5432/agents",
        ),
        embedder=embedder or _FakeEmbedder(),
    )


class TestPgVectorBackend:
    def test_satisfies_both_protocols(self):
        backend = _pg_backend()
        assert isinstance(backend, KnowledgeRetriever)
        assert isinstance(backend, KnowledgeIndex)

    def test_factory_builds_it_for_both_seams(self):
        config = KnowledgeConfig(
            orrery_knowledge_backend="pgvector", knowledge_pg_url="postgresql://x/y"
        )
        assert resolve_retriever(config) is not None
        assert resolve_index(config) is not None

    def test_missing_url_is_a_clear_error(self, monkeypatch):
        from orrery_core.knowledge.backends.pgvector import KnowledgeBackendError

        monkeypatch.delenv("DATABASE_URL", raising=False)
        backend = _pg_backend()
        backend._config.knowledge_pg_url = None
        with pytest.raises(KnowledgeBackendError, match="KNOWLEDGE_PG_URL or DATABASE_URL"):
            backend._db_url()

    def test_vectors_are_bound_parameters_not_interpolated(self):
        # The literal is passed as a bound parameter and cast in SQL, so the
        # value never reaches the statement parser.
        from orrery_core.knowledge.backends.pgvector import _vector_literal

        assert _vector_literal([0.5, -1.25]) == "[0.5,-1.25]"

    def test_embedded_text_carries_the_heading(self):
        # A chunk from the middle of a long runbook has no self-contained
        # indication of what it is about; the heading is exactly that context.
        from datetime import UTC, datetime

        from orrery_core.knowledge.backends.pgvector import PgVectorKnowledgeBackend
        from orrery_core.knowledge.models import Chunk

        chunk = Chunk(
            uri="file://r.md",
            title="Kafka ISR shrink",
            section="Recovery",
            text="Restart the broker.",
            revision="r1",
            updated_at=datetime.now(UTC),
            ordinal=0,
        )
        embedded = PgVectorKnowledgeBackend._embed_text(chunk)
        assert embedded.startswith("Kafka ISR shrink — Recovery")
        assert "Restart the broker." in embedded

    @pytest.mark.asyncio
    async def test_upsert_of_nothing_does_not_call_the_embedder(self):
        embedder = _FakeEmbedder()
        await _pg_backend(embedder).upsert([])
        assert embedder.calls == []

    @pytest.mark.asyncio
    async def test_a_short_embedder_response_is_rejected(self):
        # Zipping mismatched lists would silently attach the wrong vector to a
        # chunk, which only shows up as inexplicably bad retrieval.
        from datetime import UTC, datetime

        from orrery_core.knowledge.backends.pgvector import KnowledgeBackendError
        from orrery_core.knowledge.models import Chunk

        class _Short(_FakeEmbedder):
            async def embed(self, texts, *, kind):
                return [[0.1, 0.2, 0.3, 0.4]]

        chunks = [
            Chunk(
                uri="file://a.md",
                title="A",
                section=None,
                text=f"body {i}",
                revision="r",
                updated_at=datetime.now(UTC),
                ordinal=i,
            )
            for i in range(2)
        ]
        with pytest.raises(KnowledgeBackendError, match="1 vectors for 2 chunks"):
            await _pg_backend(_Short()).upsert(chunks)

    @pytest.mark.asyncio
    async def test_queries_are_embedded_as_queries(self):
        embedder = _FakeEmbedder()
        backend = _pg_backend(embedder)
        with patch.object(backend, "_retrieve_sync", return_value=[]):
            await backend.retrieve("isr shrink", top_k=5)
        assert embedder.calls == [(1, "query")]


# ── Confluence source ────────────────────────────────────────────────


class TestConfluenceSource:
    def test_auto_discovery_is_refused(self):
        # Retrieval runs at viewer for everyone. Indexing a space nobody
        # explicitly listed would make restricted content readable through the
        # agent, with no trace on the Confluence side.
        with pytest.raises(ValueError, match="explicit space list"):
            ConfluenceSource("https://x.atlassian.net", spaces=[], email="e", api_token="t")

    def test_build_from_env_needs_every_setting(self, monkeypatch):
        for key in (
            "KNOWLEDGE_CONFLUENCE_URL",
            "KNOWLEDGE_CONFLUENCE_SPACES",
            "KNOWLEDGE_CONFLUENCE_EMAIL",
            "KNOWLEDGE_CONFLUENCE_API_TOKEN",
        ):
            monkeypatch.delenv(key, raising=False)
        assert build_from_env() is None

        monkeypatch.setenv("KNOWLEDGE_CONFLUENCE_URL", "https://x.atlassian.net")
        monkeypatch.setenv("KNOWLEDGE_CONFLUENCE_SPACES", "SRE, OPS")
        monkeypatch.setenv("KNOWLEDGE_CONFLUENCE_EMAIL", "bot@example.com")
        monkeypatch.setenv("KNOWLEDGE_CONFLUENCE_API_TOKEN", "tok")
        source = build_from_env()
        assert source is not None
        assert source._spaces == ["SRE", "OPS"]

    @pytest.mark.asyncio
    async def test_pages_become_documents_with_version_as_revision(self):
        # The page version is what makes incremental sync possible against a
        # rate-limited API holding thousands of pages.
        payload = {
            "size": 1,
            "results": [
                {
                    "id": "12345",
                    "title": "Kafka runbook",
                    "version": {"number": 7, "when": "2026-08-01T10:00:00.000Z"},
                    "body": {"storage": {"value": "<h1>Recovery</h1><p>Restart it.</p>"}},
                }
            ],
        }
        source = ConfluenceSource(
            "https://x.atlassian.net/", spaces=["SRE"], email="e", api_token="t"
        )
        with patch.object(requests.Session, "get") as get:
            get.return_value = MagicMock(status_code=200, json=lambda: payload)
            documents = [d async for d in source.documents()]

        assert len(documents) == 1
        doc = documents[0]
        assert doc.uri == "confluence://SRE/12345"
        assert doc.revision == "7"
        assert doc.title == "Kafka runbook"
        assert "# Recovery" in doc.text
        assert doc.labels["space"] == "SRE"
        assert doc.labels["url"].endswith("/wiki/spaces/SRE/pages/12345")

    @pytest.mark.asyncio
    async def test_a_forbidden_space_fails_loudly(self):
        source = ConfluenceSource(
            "https://x.atlassian.net", spaces=["SECRET"], email="e", api_token="t"
        )
        with (
            patch.object(requests.Session, "get", return_value=MagicMock(status_code=403)),
            pytest.raises(PermissionError, match="not ACL-aware"),
        ):
            [d async for d in source.documents()]

    @pytest.mark.asyncio
    async def test_empty_pages_are_skipped(self):
        payload = {
            "size": 1,
            "results": [
                {
                    "id": "1",
                    "title": "Empty",
                    "version": {"number": 1},
                    "body": {"storage": {"value": "  "}},
                }
            ],
        }
        source = ConfluenceSource("https://x", spaces=["SRE"], email="e", api_token="t")
        with patch.object(requests.Session, "get") as get:
            get.return_value = MagicMock(status_code=200, json=lambda: payload)
            assert [d async for d in source.documents()] == []


class TestStorageFormatConversion:
    def test_headings_survive_so_chunking_still_works(self):
        text = html_to_text("<h2>Symptoms</h2><p>ISR shrinks.</p><h2>Recovery</h2><p>Restart.</p>")
        assert "## Symptoms" in text
        assert "## Recovery" in text

    def test_code_macros_become_fences(self):
        # Code blocks carry the commands an operator actually runs; the chunker
        # must see them as atomic or it will cut a command in half.
        storage = (
            '<ac:structured-macro ac:name="code"><ac:plain-text-body>'
            "<![CDATA[kubectl rollout restart sts/kafka]]>"
            "</ac:plain-text-body></ac:structured-macro>"
        )
        text = html_to_text(storage)
        assert "```" in text
        assert "kubectl rollout restart sts/kafka" in text

    def test_entities_are_unescaped_and_tags_dropped(self):
        assert html_to_text("<p>a &amp; b &lt;c&gt;</p>").strip() == "a & b <c>"

    def test_lists_become_markdown_bullets(self):
        text = html_to_text("<ul><li>first</li><li>second</li></ul>")
        assert "- first" in text
        assert "- second" in text
