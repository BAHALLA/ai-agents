"""``search_knowledge`` — the agent's read end of the knowledge layer.

Subclasses ADK's :class:`BaseRetrievalTool`, which declares a single ``query``
parameter. **Being a real tool is the whole point**, not an implementation
detail: it is what puts retrieval on the plugin chain, and the chain is what
makes indexing third-party documents safe.

- ``SafetyScreenPlugin`` neutralizes injected spans in the result. A Confluence
  page or a git-hosted runbook is attacker-reachable text the moment anyone
  outside the on-call team can edit it — the same threat model as a pod
  annotation, and already handled for tool results.
- ``PIIRedactionPlugin`` scrubs credentials. Postmortems contain pasted tokens.
- ``ToolOutputCapPlugin`` bounds a chatty retrieval.
- ``AuditPlugin`` records the query.

ADK's ``VertexAiSearchTool`` looks like the equivalent for a managed backend and
is **not**: it is model built-in grounding, appending a ``types.Retrieval`` to
the LLM request so the model retrieves server-side. There is no
``after_tool_callback``, so it silently bypasses all four protections above and
never appears in the audit log. Any managed backend must be reached through a
real tool — ``DiscoveryEngineSearchTool`` or an adapter behind
:class:`~orrery_core.knowledge.protocols.KnowledgeRetriever` — not through
grounding.
"""

from __future__ import annotations

import logging
from typing import Any

from google.adk.tools.retrieval.base_retrieval_tool import BaseRetrievalTool
from google.adk.tools.tool_context import ToolContext

from ..security.validation import validate_string
from ..tools.tool_result import ToolResult
from .protocols import KnowledgeRetriever

logger = logging.getLogger("orrery.knowledge.tool")

TOOL_NAME = "search_knowledge"

_DESCRIPTION = (
    "Search the team's own operational knowledge — runbooks, postmortems, "
    "architecture decision records and internal documentation — for guidance "
    "on a symptom, alert, or system. Use this BEFORE diagnosing from scratch: "
    "it returns what humans already wrote about this class of problem, with "
    "the source document and its age so you can judge how current it is. "
    "This does NOT read live infrastructure; use the specialist agents for that."
)

#: Characters of passage text handed to the model per hit. Full chunks would
#: re-enter the prompt on every subsequent turn of the conversation, so the
#: budget bounds ongoing cost rather than the size of one answer.
MAX_PASSAGE_CHARS = 1200

#: A document older than this is flagged in the result. Not a filter — a
#: two-year-old runbook may be the only one there is — but the model should
#: weigh it accordingly rather than quoting it as current.
STALE_AFTER_DAYS = 180


class KnowledgeSearchTool(BaseRetrievalTool):
    """Retrieval over the configured knowledge corpus."""

    def __init__(
        self,
        retriever: KnowledgeRetriever,
        *,
        top_k: int = 6,
        name: str = TOOL_NAME,
        description: str = _DESCRIPTION,
    ) -> None:
        super().__init__(name=name, description=description)
        self._retriever = retriever
        self._top_k = top_k

    async def run_async(self, *, args: dict[str, Any], tool_context: ToolContext) -> Any:
        query = args.get("query")
        if err := validate_string(query, "query", min_len=2, max_len=500):
            return err
        assert isinstance(query, str)  # noqa: S101 — narrowed by validate_string

        try:
            passages = await self._retriever.retrieve(query, top_k=self._top_k)
        except Exception as exc:  # noqa: BLE001 — surfaced as a tool error, not a crash
            logger.exception("knowledge search failed", extra={"query_len": len(query)})
            return ToolResult.error(
                f"Knowledge search is unavailable: {exc}",
                error_type="KnowledgeBackendError",
                hints=[
                    "Check ORRERY_KNOWLEDGE_BACKEND and the backend's connection settings",
                    "Verify the corpus has been indexed (`make knowledge-sync`)",
                    "Continue the diagnosis from live signals; do not treat this as "
                    "evidence that no runbook exists",
                ],
            ).to_dict()

        if not passages:
            # Deliberately distinct from the error above: nothing matched is a
            # fact about the corpus, and the model should say so rather than
            # implying the search was broken.
            return ToolResult.ok(
                f"No indexed documentation matches '{query}'.",
                query=query,
                results=[],
                hints=["Proceed from live signals; consider writing a runbook afterwards"],
            ).to_dict()

        results = [
            {
                "text": _clip(passage.text),
                "source": passage.uri,
                "title": passage.title,
                "section": passage.section,
                "revision": passage.revision,
                "age_days": (age := passage.age_days()),
                "stale": age > STALE_AFTER_DAYS,
                "score": round(passage.score, 4),
            }
            for passage in passages
        ]
        stale = sum(1 for r in results if r["stale"])
        hints = [
            "Cite the `source` of any claim you take from these passages",
        ]
        if stale:
            hints.append(
                f"{stale} of {len(results)} passages come from documents not updated in "
                f"over {STALE_AFTER_DAYS} days — verify against live state before acting"
            )
        return ToolResult.ok(
            f"Found {len(results)} passage(s) for '{query}'.",
            query=query,
            results=results,
            hints=hints,
        ).to_dict()


def _clip(text: str) -> str:
    if len(text) <= MAX_PASSAGE_CHARS:
        return text
    return text[:MAX_PASSAGE_CHARS].rstrip() + " …[truncated]"
