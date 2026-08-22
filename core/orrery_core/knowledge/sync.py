"""Corpus sync: sources → chunks → index.

Indexing is a **build-time** action, never lazy at request time. A first query
that silently triggers a full crawl is an outage waiting for its moment, and it
would put an unbounded, unaudited operation on the agent's critical path.

Sync is idempotent and revision-driven. A document whose revision matches what
the index already holds is skipped entirely; one that changed is re-chunked,
upserted, and then has its *stale* chunks deleted — which is what keeps an edit
from leaving orphans behind. A document that shrinks from nine chunks to four
would otherwise keep answering queries from five slices that no longer exist in
the source, and those are the hardest wrong answers to notice.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from .chunking import chunk_document
from .config import KnowledgeConfig
from .protocols import KnowledgeIndex, KnowledgeSource

logger = logging.getLogger("orrery.knowledge.sync")


@dataclass
class SyncReport:
    """What one sync pass did, for the CLI and for tests."""

    indexed: int = 0
    skipped: int = 0
    chunks: int = 0
    pruned: int = 0
    removed_documents: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"indexed {self.indexed} document(s) as {self.chunks} chunk(s); "
            f"skipped {self.skipped} unchanged; pruned {self.pruned} stale chunk(s); "
            f"removed {self.removed_documents} deleted document(s)"
            + (f"; {len(self.errors)} error(s)" if self.errors else "")
        )


async def sync_sources(
    sources: Sequence[KnowledgeSource],
    index: KnowledgeIndex,
    *,
    config: KnowledgeConfig | None = None,
    prune_deleted: bool = True,
) -> SyncReport:
    """Index every document from *sources* into *index*.

    Args:
        sources: Connectors to walk, in order.
        index: The write side to populate.
        config: Chunking budgets; defaults are read from the environment.
        prune_deleted: Remove indexed documents no source produced any more.
            A runbook deleted from the repo must stop being retrievable, or the
            index keeps recommending a procedure the team retired. Disable only
            when syncing a subset of sources, where "absent" does not mean
            "deleted".

    Returns:
        A :class:`SyncReport`. Per-document failures are collected rather than
        raised: one unreadable page must not abandon the rest of the corpus.
    """
    cfg = config or KnowledgeConfig()
    report = SyncReport()

    await index.ensure_ready()
    known = await _known_revisions(index)
    seen: set[str] = set()

    for source in sources:
        try:
            async for document in source.documents():
                seen.add(document.uri)
                if known.get(document.uri) == document.revision:
                    report.skipped += 1
                    continue
                try:
                    chunks = chunk_document(
                        document,
                        max_chars=cfg.orrery_knowledge_max_chars,
                        overlap_chars=cfg.orrery_knowledge_overlap_chars,
                    )
                    if not chunks:
                        continue
                    await index.upsert(chunks)
                    report.pruned += await index.delete_stale(document.uri, document.revision)
                    report.indexed += 1
                    report.chunks += len(chunks)
                except Exception as exc:  # noqa: BLE001 — one bad doc, not the corpus
                    logger.exception("failed to index document", extra={"uri": document.uri})
                    report.errors.append(f"{document.uri}: {exc}")
        except Exception as exc:  # noqa: BLE001 — one bad source, not the run
            logger.exception("knowledge source failed", extra={"source": source.name})
            report.errors.append(f"source {source.name}: {exc}")

    if prune_deleted and not report.errors:
        # Only prune on a clean pass. A source that failed halfway looks
        # identical to one whose documents were all deleted, and acting on that
        # ambiguity would empty the index because a Confluence token expired.
        for uri in set(known) - seen:
            report.removed_documents += 1
            report.pruned += await index.delete_by_uri(uri)

    logger.info("knowledge sync complete", extra={"summary": report.summary()})
    return report


async def _known_revisions(index: KnowledgeIndex) -> dict[str, str]:
    """Current uri → revision map, when the backend can report one.

    Optional capability: a backend without it re-indexes everything, which is
    correct but slower.
    """
    reader = getattr(index, "indexed_revisions", None)
    if reader is None:
        return {}
    try:
        return await reader()
    except Exception:  # noqa: BLE001 — a missing index on first run is normal
        logger.debug("could not read indexed revisions; treating corpus as empty")
        return {}
