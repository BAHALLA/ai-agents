"""Chunking behaviour that retrieval quality depends on."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from orrery_core.knowledge.chunking import chunk_document
from orrery_core.knowledge.models import Document

NOW = datetime(2026, 8, 1, tzinfo=UTC)


def _doc(text: str, *, uri: str = "file://runbook.md") -> Document:
    return Document(
        uri=uri,
        title="Runbook",
        text=text,
        revision="rev1",
        updated_at=NOW,
        labels={"collection": "runbooks"},
    )


def test_splits_on_headings_and_records_the_section():
    chunks = chunk_document(
        _doc("# Title\n\nIntro.\n\n## Symptoms\n\nISR shrinks.\n\n## Recovery\n\nRestart it.\n")
    )
    sections = [c.section for c in chunks]
    assert sections == ["Title", "Symptoms", "Recovery"]
    # A citation says "§ Recovery", so the recovery text must not have leaked
    # into the symptoms chunk.
    recovery = next(c for c in chunks if c.section == "Recovery")
    assert "Restart it." in recovery.text
    assert "ISR shrinks" not in recovery.text


def test_preamble_before_any_heading_is_still_indexed():
    chunks = chunk_document(_doc("Summary sentence with the real answer.\n\n# Later\n\nBody.\n"))
    assert chunks[0].section is None
    assert "Summary sentence" in chunks[0].text


def test_ordinals_are_unique_and_ids_are_stable_across_runs():
    doc = _doc("# A\n\nalpha\n\n# B\n\nbeta\n")
    first = chunk_document(doc)
    second = chunk_document(doc)
    assert [c.ordinal for c in first] == list(range(len(first)))
    # Re-indexing an unchanged document must overwrite in place, not append a
    # second copy — which only holds if the ids are deterministic.
    assert [c.id for c in first] == [c.id for c in second]
    assert len({c.id for c in first}) == len(first)


def test_different_documents_do_not_collide_on_id():
    a = chunk_document(_doc("# A\n\nsame body\n", uri="file://a.md"))
    b = chunk_document(_doc("# A\n\nsame body\n", uri="file://b.md"))
    assert a[0].id != b[0].id


def test_a_heading_inside_a_code_fence_is_not_a_section():
    # '#' opens a shell comment; treating it as a heading would split a command
    # in half and index an unrunnable fragment.
    text = "## Recovery\n\n```bash\n# restart the broker\nkubectl rollout restart sts/kafka\n```\n"
    chunks = chunk_document(_doc(text))
    assert [c.section for c in chunks] == ["Recovery"]
    assert "kubectl rollout restart" in chunks[0].text


def test_code_fences_are_never_split_even_when_oversized():
    fence = "```yaml\n" + "\n".join(f"key{i}: value{i}" for i in range(200)) + "\n```"
    chunks = chunk_document(_doc(f"# Manifest\n\n{fence}\n"), max_chars=200, overlap_chars=0)
    holding = [c for c in chunks if "key0:" in c.text]
    assert len(holding) == 1
    # A truncated manifest is worse than an oversized chunk: the model would
    # act on the fragment.
    assert holding[0].text.count("```") == 2
    assert "key199: value199" in holding[0].text


def test_long_section_is_packed_with_overlap():
    paragraphs = "\n\n".join(f"Paragraph {i} " + "word " * 40 for i in range(10))
    chunks = chunk_document(_doc(f"# Long\n\n{paragraphs}\n"), max_chars=600, overlap_chars=80)
    assert len(chunks) > 1
    assert all(c.section == "Long" for c in chunks)
    # Overlap carries trailing context forward so a definition at the tail of
    # one chunk is not orphaned from the sentence using it in the next.
    assert chunks[1].text[:40] in chunks[0].text


def test_empty_and_whitespace_documents_produce_nothing():
    assert chunk_document(_doc("")) == []
    assert chunk_document(_doc("   \n\n  \n")) == []


def test_chunks_inherit_document_provenance():
    chunk = chunk_document(_doc("# T\n\nbody\n"))[0]
    assert chunk.uri == "file://runbook.md"
    assert chunk.revision == "rev1"
    assert chunk.updated_at == NOW
    assert chunk.labels["collection"] == "runbooks"


@pytest.mark.parametrize(
    ("max_chars", "overlap"),
    [(0, 0), (-1, 0), (100, 100), (100, 200), (100, -1)],
)
def test_invalid_budgets_are_rejected(max_chars, overlap):
    with pytest.raises(ValueError):
        chunk_document(_doc("# T\n\nbody\n"), max_chars=max_chars, overlap_chars=overlap)
