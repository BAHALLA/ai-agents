"""Heading-aware markdown chunking.

Splits on markdown headings first, then packs paragraphs into size-bounded
chunks within each section. Two decisions are worth recording:

**Sections before size.** A runbook's "Recovery" section is a semantic unit; a
fixed-width window that straddles the boundary between "Symptoms" and
"Recovery" retrieves half of each and reads as neither. Splitting on headings
also gives every chunk a ``section`` label for free, which is what makes a
citation useful — "§ Recovery" beats "chunk 47".

**Characters, not tokens.** Budgets are measured in characters so chunking has
no tokenizer dependency and stays deterministic across model providers. The
ratio is roughly 4 characters per token for English prose, so the default
1,600-character budget lands near 400 tokens. Being approximate is fine: the
budget exists to keep passages readable and bound the cost of stuffing several
into a prompt, not to hit an exact context figure.

Fenced code blocks are never split. A truncated YAML manifest or half a shell
pipeline is worse than an oversized chunk — the model will happily act on the
fragment.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence

from .models import Chunk, Document

#: Target size for one chunk, in characters (~400 tokens of English prose).
DEFAULT_MAX_CHARS = 1600

#: Trailing context repeated at the start of the next chunk when one section
#: spills across several. Cheap insurance against a definition landing at the
#: tail of chunk *n* while the sentence that uses it opens chunk *n+1*.
DEFAULT_OVERLAP_CHARS = 200

#: ATX headings only (``#`` … ``######``). Setext underlining is rare in the
#: docs this indexes and ambiguous to parse line-by-line.
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")

_FENCE = re.compile(r"^\s*(```|~~~)")

#: Paragraph break: a blank line, optionally carrying whitespace.
_PARA_SPLIT = re.compile(r"\n[ \t]*\n")


def _split_sections(text: str) -> Iterator[tuple[str | None, str]]:
    """Yield ``(section_title, body)`` pairs, respecting code fences.

    Text before the first heading is yielded with a ``None`` title — a document
    whose preamble carries the summary should still be retrievable.
    """
    current: str | None = None
    buffer: list[str] = []
    in_fence = False
    fence_marker = ""

    for line in text.splitlines():
        fence = _FENCE.match(line)
        if fence:
            marker = fence.group(1)
            if not in_fence:
                in_fence, fence_marker = True, marker
            elif marker == fence_marker:
                in_fence = False
            buffer.append(line)
            continue

        # A '#' inside a fence is a shell comment or a YAML key, not a heading.
        heading = None if in_fence else _HEADING.match(line)
        if heading:
            body = "\n".join(buffer).strip()
            if body:
                yield current, body
            current = heading.group(2).strip()
            buffer = []
        else:
            buffer.append(line)

    body = "\n".join(buffer).strip()
    if body:
        yield current, body


def _atomic_blocks(body: str) -> list[str]:
    """Split a section body into blocks that may never be divided further.

    Paragraphs split freely; a fenced code block is one block however long.
    """
    blocks: list[str] = []
    fenced: list[str] = []
    plain: list[str] = []
    in_fence = False
    fence_marker = ""

    def flush_plain() -> None:
        text = "\n".join(plain).strip()
        plain.clear()
        if text:
            blocks.extend(p.strip() for p in _PARA_SPLIT.split(text) if p.strip())

    for line in body.splitlines():
        fence = _FENCE.match(line)
        if fence:
            marker = fence.group(1)
            if not in_fence:
                flush_plain()
                in_fence, fence_marker = True, marker
                fenced = [line]
            elif marker == fence_marker:
                fenced.append(line)
                blocks.append("\n".join(fenced))
                fenced, in_fence = [], False
            else:
                fenced.append(line)
            continue
        (fenced if in_fence else plain).append(line)

    # An unterminated fence still has to reach the index; keep it whole.
    if fenced:
        blocks.append("\n".join(fenced))
    flush_plain()
    return blocks


def _pack(blocks: Sequence[str], max_chars: int, overlap_chars: int) -> list[str]:
    """Greedily pack blocks into chunks no larger than *max_chars*.

    A single block over budget (a long code fence) becomes its own oversized
    chunk rather than being cut.
    """
    chunks: list[str] = []
    current: list[str] = []
    size = 0

    for block in blocks:
        block_len = len(block)
        if current and size + block_len + 2 > max_chars:
            chunks.append("\n\n".join(current))
            tail = chunks[-1][-overlap_chars:] if overlap_chars else ""
            # Only carry overlap forward as prose — repeating a code fragment
            # would produce an unbalanced fence in the next chunk.
            current = [tail] if tail and not tail.lstrip().startswith(("```", "~~~")) else []
            size = len(current[0]) if current else 0
        current.append(block)
        size += block_len + 2

    if current:
        chunks.append("\n\n".join(current))
    return [c for c in (chunk.strip() for chunk in chunks) if c]


def chunk_document(
    document: Document,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[Chunk]:
    """Split *document* into retrievable chunks.

    Ordinals are assigned across the whole document, not per section, so
    :attr:`Chunk.id` stays stable and unique regardless of how sections split.

    Returns an empty list for a document with no substantive text — an empty
    placeholder page should not occupy a slot in every result set.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("overlap_chars must be non-negative and smaller than max_chars")

    chunks: list[Chunk] = []
    ordinal = 0
    for section, body in _split_sections(document.text):
        for text in _pack(_atomic_blocks(body), max_chars, overlap_chars):
            chunks.append(
                Chunk(
                    uri=document.uri,
                    title=document.title,
                    section=section,
                    text=text,
                    revision=document.revision,
                    updated_at=document.updated_at,
                    ordinal=ordinal,
                    labels=dict(document.labels),
                )
            )
            ordinal += 1
    return chunks
