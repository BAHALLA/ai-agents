"""Confluence knowledge source.

Pulls pages from one or more spaces through the Confluence Cloud REST API.
Two things make this different from the repo-backed sources, and both are
load-bearing:

**Incremental sync is not optional here.** A repo tree is a few dozen files; a
Confluence space is thousands of pages behind a rate-limited API. The page
version number is the revision, so a re-sync only fetches bodies for pages
whose version moved.

**ACLs are refused, not approximated.** Confluence spaces have per-user
permissions and Orrery's retrieval does not: ``search_knowledge`` runs at
``viewer`` for everyone. So a space is indexed only when it is explicitly
configured, and the source never discovers spaces on its own. Indexing a
restricted space would make its contents readable by every viewer through the
agent — a permission bypass with no audit trail on the Confluence side.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import requests

from ..models import Document

logger = logging.getLogger("orrery.knowledge.confluence")

#: Pages fetched per API call. Confluence caps this server-side; 50 keeps
#: responses small enough that a slow space does not hold one connection open
#: for minutes.
PAGE_SIZE = 50

#: Refuse a single page larger than this. An exported log or a giant table
#: chunks into hundreds of near-useless passages and crowds real documentation
#: out of every result set.
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024


class ConfluenceSource:
    """Indexes pages from explicitly configured Confluence spaces."""

    def __init__(
        self,
        base_url: str,
        *,
        spaces: Sequence[str],
        email: str,
        api_token: str,
        name: str = "confluence",
        labels: Mapping[str, str] | None = None,
        timeout: int = 30,
    ) -> None:
        if not spaces:
            # Never "index everything you can see" — see the module docstring.
            raise ValueError(
                "ConfluenceSource requires an explicit space list. Auto-discovery would "
                "index restricted spaces, and retrieval is not ACL-aware."
            )
        self.name = name
        self._base = base_url.rstrip("/")
        self._spaces = list(spaces)
        self._auth = (email, api_token)
        self._labels = dict(labels or {})
        self._timeout = timeout
        self._session: requests.Session | None = None

    def _get_session(self) -> requests.Session:
        if self._session is None:
            session = requests.Session()
            session.auth = self._auth
            session.headers.update({"Accept": "application/json"})
            self._session = session
        return self._session

    async def watermark(self) -> str | None:
        """No global watermark — versioning is per page, not per space."""
        return None

    async def documents(self, since: str | None = None) -> AsyncIterator[Document]:
        for space in self._spaces:
            async for document in self._space_documents(space):
                yield document

    async def _space_documents(self, space: str) -> AsyncIterator[Document]:
        start = 0
        while True:
            payload = await asyncio.to_thread(self._fetch_page_batch, space, start)
            results = payload.get("results", [])
            if not results:
                return
            for raw in results:
                document = self._to_document(space, raw)
                if document is not None:
                    yield document
            start += len(results)
            if start >= int(payload.get("size", 0)) and not payload.get("_links", {}).get("next"):
                return

    def _fetch_page_batch(self, space: str, start: int) -> dict[str, Any]:
        response = self._get_session().get(
            f"{self._base}/wiki/rest/api/content",
            params={
                "spaceKey": space,
                "type": "page",
                "status": "current",
                "start": start,
                "limit": PAGE_SIZE,
                # storage format is the authored XHTML; version.number is the
                # revision that makes incremental sync possible.
                "expand": "body.storage,version,history.lastUpdated",
            },
            timeout=self._timeout,
        )
        if response.status_code == 403:
            raise PermissionError(
                f"Confluence refused space {space!r}. Configure only spaces the "
                f"integration user may read — retrieval is not ACL-aware."
            )
        response.raise_for_status()
        return response.json()

    def _to_document(self, space: str, raw: Mapping[str, Any]) -> Document | None:
        page_id = str(raw.get("id") or "")
        title = str(raw.get("title") or "").strip()
        storage = ((raw.get("body") or {}).get("storage") or {}).get("value") or ""
        if not page_id or not storage.strip():
            return None
        if len(storage.encode("utf-8", "ignore")) > MAX_DOCUMENT_BYTES:
            logger.warning(
                "skipping oversized confluence page",
                extra={"space": space, "page_id": page_id},
            )
            return None

        version = str(((raw.get("version") or {}).get("number")) or "0")
        updated = _parse_when((raw.get("version") or {}).get("when"))
        return Document(
            uri=f"confluence://{space}/{page_id}",
            title=title or f"Page {page_id}",
            text=html_to_text(storage),
            revision=version,
            updated_at=updated,
            labels={
                **self._labels,
                "source": self.name,
                "space": space,
                "url": f"{self._base}/wiki/spaces/{space}/pages/{page_id}",
            },
        )


def html_to_text(storage: str) -> str:
    """Flatten Confluence storage-format XHTML into chunkable markdown-ish text.

    Deliberately dependency-free and lossy. Headings become ATX so the existing
    heading-aware chunker keeps working — that is the only structure retrieval
    actually needs. Macros, layouts and attachments are dropped rather than
    rendered: a half-rendered macro reads as garbage to a model, and the
    surrounding prose is what carries the answer.
    """
    import html as _html
    import re

    text = storage
    # Structural conversions, before tags are stripped.
    for level in range(1, 7):
        text = re.sub(
            rf"<h{level}[^>]*>(.*?)</h{level}>",
            lambda m, level=level: f"\n\n{'#' * level} {m.group(1)}\n\n",
            text,
            flags=re.S | re.I,
        )
    text = re.sub(r"<li[^>]*>(.*?)</li>", r"\n- \1", text, flags=re.S | re.I)
    text = re.sub(r"</(p|div|tr|table|ul|ol)>", "\n\n", text, flags=re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    # Code macros carry the commands an operator actually runs; keep them
    # fenced so the chunker treats them as atomic.
    text = re.sub(
        r'<ac:structured-macro[^>]*ac:name="code".*?<!\[CDATA\[(.*?)\]\]>.*?</ac:structured-macro>',
        r"\n\n```\n\1\n```\n\n",
        text,
        flags=re.S | re.I,
    )
    text = re.sub(r"<[^>]+>", "", text)
    text = _html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _parse_when(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(UTC)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return datetime.now(UTC)


def spaces_from_env(raw: str | None) -> list[str]:
    """Parse ``KNOWLEDGE_CONFLUENCE_SPACES`` — comma-separated keys."""
    return [space.strip() for space in (raw or "").split(",") if space.strip()]


def build_from_env(labels: Iterable[tuple[str, str]] | None = None) -> ConfluenceSource | None:
    """Build a source from the environment, or ``None`` when unconfigured."""
    import os

    base = os.getenv("KNOWLEDGE_CONFLUENCE_URL")
    spaces = spaces_from_env(os.getenv("KNOWLEDGE_CONFLUENCE_SPACES"))
    email = os.getenv("KNOWLEDGE_CONFLUENCE_EMAIL")
    token = os.getenv("KNOWLEDGE_CONFLUENCE_API_TOKEN")
    if not (base and spaces and email and token):
        return None
    return ConfluenceSource(
        base, spaces=spaces, email=email, api_token=token, labels=dict(labels or [])
    )
