"""The two knowledge seams.

Ingestion and retrieval are **separate protocols** rather than one "RAG
provider" interface, because a managed vendor owns both halves and a
self-hosted store owns neither. Vertex AI Search ingests through its own
connectors *and* answers queries; Elasticsearch and pgvector do nothing until
we write both sides. A single interface would therefore force every managed
backend to stub out methods it has no business implementing.

So:

- :class:`KnowledgeSource` — where documents come from (filesystem, git,
  Confluence). Only meaningful for backends we populate ourselves.
- :class:`KnowledgeRetriever` — how a query becomes ranked passages. Every
  backend implements this; it is the only half the agent touches.
- :class:`KnowledgeIndex` — how chunks get written. A backend that omits it is
  declaring "my ingestion is somebody else's job", and ``knowledge-sync`` skips
  it rather than failing.

Adding Notion means writing one :class:`KnowledgeSource`. Standardising on
Azure AI Search means writing one :class:`KnowledgeRetriever`. Neither touches
an agent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Protocol, runtime_checkable

from .models import Chunk, Document, Passage


@runtime_checkable
class KnowledgeSource(Protocol):
    """Produces documents for indexing."""

    #: Short stable identifier used in logs and sync reports (``runbooks``,
    #: ``adr``, ``confluence-sre``).
    name: str

    def documents(self, since: str | None = None) -> AsyncIterator[Document]:
        """Yield documents, optionally only those changed since a token.

        Args:
            since: An opaque incremental-sync token this source previously
                returned via :meth:`watermark` — a commit sha for git, a
                timestamp for a REST API. Sources that cannot do incremental
                sync ignore it and yield everything; correctness must never
                depend on the optimisation, only cost.

        Returns:
            An async iterator so a large corpus streams rather than
            materialising in memory. Confluence spaces run to thousands of
            pages, and the sync process is not the place to hold them all.
        """
        ...

    async def watermark(self) -> str | None:
        """The token to pass as ``since`` on the next sync, or ``None``.

        Returning ``None`` means "no incremental support"; the next run does a
        full pass.
        """
        ...


@runtime_checkable
class KnowledgeRetriever(Protocol):
    """Answers queries with ranked, provenance-carrying passages."""

    async def retrieve(
        self,
        query: str,
        *,
        top_k: int,
        labels: Mapping[str, str] | None = None,
    ) -> list[Passage]:
        """Return at most *top_k* passages, best first.

        Args:
            query: Natural-language search text.
            top_k: Hard ceiling on returned passages. Retrieval results are
                re-sent to the model on every subsequent turn, so this bounds
                ongoing cost, not just one response.
            labels: Exact-match filters over :attr:`Document.labels`. All
                supplied labels must match (AND).

        Returns:
            Ranked passages, possibly empty. An empty list means "nothing
            matched" — a backend that cannot be reached raises instead, so the
            caller can tell the two apart.
        """
        ...


@runtime_checkable
class KnowledgeIndex(Protocol):
    """Write side — implemented only by backends we populate ourselves."""

    async def ensure_ready(self) -> None:
        """Create or migrate whatever the backend needs. Idempotent."""
        ...

    async def upsert(self, chunks: Sequence[Chunk]) -> None:
        """Insert or replace chunks, keyed by :attr:`Chunk.id`.

        Must be idempotent: re-syncing an unchanged document rewrites the same
        ids with the same content rather than appending duplicates.
        """
        ...

    async def delete_by_uri(self, uri: str) -> int:
        """Remove every chunk of one document. Returns the number deleted."""
        ...

    async def delete_stale(self, uri: str, revision: str) -> int:
        """Remove chunks of *uri* whose revision differs from *revision*.

        Called after upserting a document, and it is what keeps an edit from
        leaving orphans: a document that shrinks from nine chunks to four would
        otherwise keep answering queries from the five that no longer exist in
        the source.
        """
        ...
