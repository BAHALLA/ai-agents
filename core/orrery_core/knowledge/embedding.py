"""Provider-agnostic embeddings, mirroring ``resolve_model()``.

Only backends that build their own vectors need this — a managed retrieval
service embeds on its own side. It exists as a seam for the same reason the LLM
layer does: a GCP-only embedder would quietly re-import the lock-in that
``resolve_model()`` was written to avoid, making the Ollama and Claude
deployments second-class in their knowledge layer even though their chat layer
is fully supported.

Two implementations cover the field, because LiteLLM already fans out:

- Gemini through ``google-genai`` directly, since the SDK is already a
  dependency and its embedding endpoint takes a task type that measurably
  improves asymmetric retrieval.
- Everything else — OpenAI, Ollama, Cohere, Azure, Bedrock — through LiteLLM's
  ``aembedding``, which is also already a dependency.

**Document and query vectors are not interchangeable.** Retrieval is asymmetric:
a question and the passage answering it are not paraphrases of each other, and
providers that expose a task type produce measurably better neighbours when
told which side they are embedding. The ``kind`` argument carries that, and a
provider that ignores it simply loses the improvement rather than breaking.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from typing import Literal, Protocol, runtime_checkable

logger = logging.getLogger("orrery.knowledge.embedding")

#: Which side of the asymmetric pair a batch belongs to.
EmbedKind = Literal["document", "query"]

#: Default model per provider, and the dimensions it produces. The dimension
#: must match the pgvector column, so it is validated at index creation rather
#: than discovered when the first query returns a dimension-mismatch error.
DEFAULT_MODELS: dict[str, tuple[str, int]] = {
    "gemini": ("gemini-embedding-001", 3072),
    "openai": ("openai/text-embedding-3-small", 1536),
    "ollama": ("ollama/nomic-embed-text", 768),
}


@runtime_checkable
class Embedder(Protocol):
    """Turns text into vectors."""

    #: Vector width this embedder produces. Must match the index column.
    dimensions: int

    async def embed(self, texts: Sequence[str], *, kind: EmbedKind) -> list[list[float]]:
        """Embed a batch, preserving input order."""
        ...


class EmbeddingError(RuntimeError):
    """The embedding provider could not be reached or rejected the request."""


class GeminiEmbedder:
    """``google-genai`` embeddings with retrieval task types."""

    #: Gemini's own names for the two sides of an asymmetric retrieval pair.
    _TASK = {"document": "RETRIEVAL_DOCUMENT", "query": "RETRIEVAL_QUERY"}

    def __init__(self, model: str, dimensions: int) -> None:
        self.model = model
        self.dimensions = dimensions
        self._client = None

    def _get_client(self):
        if self._client is None:
            from google import genai

            # No explicit credentials: the SDK resolves GOOGLE_API_KEY or
            # Vertex ADC exactly as it does for the chat model, so a deployment
            # configures auth once.
            self._client = genai.Client()
        return self._client

    async def embed(self, texts: Sequence[str], *, kind: EmbedKind) -> list[list[float]]:
        if not texts:
            return []
        from google.genai import types

        try:
            response = await self._get_client().aio.models.embed_content(
                model=self.model,
                contents=list(texts),
                config=types.EmbedContentConfig(
                    task_type=self._TASK[kind],
                    output_dimensionality=self.dimensions,
                ),
            )
        except Exception as exc:  # noqa: BLE001 — surfaced as a typed error
            raise EmbeddingError(f"Gemini embedding failed: {exc}") from exc
        return [list(item.values or []) for item in (response.embeddings or [])]


class LiteLlmEmbedder:
    """Everything else, through LiteLLM's provider fan-out.

    LiteLLM has no portable task-type parameter, so ``kind`` is accepted and
    ignored here. That costs some retrieval quality on providers that support
    it natively and costs nothing on those that do not — the alternative,
    per-provider special-casing, would rebuild the fan-out this exists to use.
    """

    def __init__(self, model: str, dimensions: int) -> None:
        self.model = model
        self.dimensions = dimensions

    async def embed(self, texts: Sequence[str], *, kind: EmbedKind) -> list[list[float]]:
        if not texts:
            return []
        import litellm

        try:
            response = await litellm.aembedding(model=self.model, input=list(texts))
        except Exception as exc:  # noqa: BLE001 — surfaced as a typed error
            raise EmbeddingError(f"Embedding failed for model {self.model}: {exc}") from exc
        # LiteLLM normalises to the OpenAI response shape but does not promise
        # ordering, so sort by index rather than trusting the list order.
        items = sorted(response["data"], key=lambda d: d.get("index", 0))
        return [list(item["embedding"]) for item in items]


def resolve_embedder(
    *, provider: str | None = None, model: str | None = None, dimensions: int | None = None
) -> Embedder:
    """Build the configured embedder from ``EMBEDDING_PROVIDER`` / ``EMBEDDING_MODEL``.

    Defaults to the same provider as the chat model (``MODEL_PROVIDER``), which
    is almost always what a deployment wants and means one fewer variable to
    set. Dimensions default per model and can be overridden — but the value must
    match the index column, so changing it requires a re-index.
    """
    resolved_provider = (
        (provider or os.getenv("EMBEDDING_PROVIDER") or os.getenv("MODEL_PROVIDER") or "gemini")
        .strip()
        .lower()
    )

    default_model, default_dims = DEFAULT_MODELS.get(
        resolved_provider, (f"{resolved_provider}/unknown", 1536)
    )
    resolved_model = model or os.getenv("EMBEDDING_MODEL") or default_model
    resolved_dims = dimensions or int(os.getenv("EMBEDDING_DIMENSIONS") or default_dims)

    logger.info(
        "embedder resolved",
        extra={"provider": resolved_provider, "model": resolved_model, "dims": resolved_dims},
    )
    if resolved_provider == "gemini":
        return GeminiEmbedder(resolved_model, resolved_dims)
    return LiteLlmEmbedder(_prefix(resolved_model, resolved_provider), resolved_dims)


def _prefix(model_name: str, provider: str) -> str:
    """Ensure LiteLLM sees a provider-qualified model id.

    Mirrors ``_prefix_provider`` in ``agent/base.py``: a bare ``nomic-embed-text``
    is ambiguous to LiteLLM, and the failure is a confusing routing error rather
    than a clear one.
    """
    return model_name if "/" in model_name else f"{provider}/{model_name}"
