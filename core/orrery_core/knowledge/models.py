"""Records that cross the knowledge seams: Document, Chunk, Passage.

Three small frozen records, deliberately independent of any backend or source.
A connector produces :class:`Document`; chunking turns one into many
:class:`Chunk`; a retriever returns :class:`Passage`.

The one non-obvious design rule is that **provenance is required, not optional**
on every record. A retrieved passage that cannot say where it came from is
indistinguishable from a hallucination at 03:00, and that is the failure mode
that makes an SRE team stop trusting retrieval altogether. ``revision`` is
required for the same reason: a runbook indexed six months ago and edited since
is worse than no runbook, so the age has to travel with the text.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

#: Separator used when deriving a chunk's stable id. Chosen because it cannot
#: appear in a URI, so ``uri`` and ``ordinal`` can never be ambiguous.
_ID_SEP = "\x1f"


@dataclass(frozen=True, slots=True)
class Document:
    """One source document, before chunking.

    Attributes:
        uri: Stable identity across syncs, scheme-prefixed by the source that
            produced it (``file://…``, ``git://remote@path``,
            ``confluence://SPACE/12345``). Re-syncing the same document must
            produce the same URI or the index accumulates duplicates.
        title: Human-readable name, shown in citations.
        text: Full document body (markdown or plain text).
        revision: Source-defined version marker — a commit sha, a Confluence
            page version, or ``mtime:size`` for a bare file. Sync compares this
            to decide whether a re-index is needed, so it must change whenever
            the content does and *not* change when it doesn't.
        updated_at: When the source last modified the document.
        labels: Free-form filters (``space``, ``repo``, ``system``, …). Kept as
            a flat string map because every backend can index that; anything
            richer would not survive the seam.
    """

    uri: str
    title: str
    text: str
    revision: str
    updated_at: datetime
    labels: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Chunk:
    """A retrievable slice of a :class:`Document`.

    Carries the parent document's provenance rather than a reference to it, so
    a backend can return a hit without a second lookup and a passage stays
    self-describing after it leaves the store.
    """

    uri: str
    title: str
    section: str | None
    text: str
    revision: str
    updated_at: datetime
    ordinal: int
    labels: Mapping[str, str] = field(default_factory=dict)

    @property
    def id(self) -> str:
        """Deterministic document id for the backing store.

        Derived from ``(uri, ordinal)`` so that re-indexing an unchanged
        document overwrites its chunks in place instead of appending a second
        copy. Hashed rather than concatenated because backends impose id length
        and character limits that a raw URI would breach.
        """
        raw = f"{self.uri}{_ID_SEP}{self.ordinal}".encode()
        return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class Passage:
    """A scored search hit, ready to hand to the model.

    ``score`` is backend-relative and only meaningful for ranking *within* one
    result set — never compare it across backends or across queries.
    """

    text: str
    uri: str
    title: str
    section: str | None
    revision: str
    updated_at: datetime
    score: float

    def age_days(self, *, now: datetime | None = None) -> int:
        """Whole days since the source last modified this passage's document.

        Surfaced in the tool result so the model can discount a stale runbook
        rather than quoting it with the same confidence as a fresh one.
        """
        reference = now or datetime.now(UTC)
        updated = self.updated_at
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=UTC)
        return max(0, (reference - updated).days)
