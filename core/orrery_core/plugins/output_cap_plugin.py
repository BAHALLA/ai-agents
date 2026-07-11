"""ToolOutputCapPlugin — bound a single tool result's size.

A tool that returns a very large payload (a chatty ``kubectl logs``, a wide
Elasticsearch result, a long Prometheus range) would otherwise be appended
verbatim to the session and shipped to the model on the next turn. Past the
Gemini/Vertex request limit (~10 MiB) that fails the *whole* run with
``400 INVALID_ARGUMENT: Request payload size exceeds the limit``. This plugin
caps each tool result to a byte budget, trimming the largest string field(s) or
the longest list of records (keeping whole elements, so the JSON stays valid)
and leaving a clear marker so the model narrows its query — rather than letting
one oversized result sink the conversation.

Registered **after** the observability plugins (audit/activity/metrics) because
ADK's plugin chain early-exits on the first non-``None`` return: the cap only
returns a replacement for oversized results, so running it last lets the other
plugins still observe every call.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger("orrery.plugins")

#: Default per-result cap (4 MiB). The Gemini/Vertex request limit is ~10 MiB and
#: exceeding it fails the whole run with ``400 INVALID_ARGUMENT``. The session
#: re-sends every accumulated tool result plus the prompt and history each turn,
#: so a single result must still leave room for the rest. NOTE: this is a
#: *static per-result* cap — if several multi-MiB results pile up in one session
#: they can still approach the limit; if the 400 appears, lower this (e.g. 2 MiB)
#: or make the cap adaptive on the request's *remaining* budget.
DEFAULT_MAX_TOOL_RESULT_BYTES = 4 * 1024 * 1024


def _serialized(result: Any) -> str:
    """Render a tool result as text: a plain string as-is, else compact JSON."""
    if isinstance(result, str):
        return result
    return json.dumps(result, default=str, ensure_ascii=False)


def _byte_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _truncate_to_bytes(text: str, budget: int) -> str:
    """First ``budget`` UTF-8 bytes of ``text``, not splitting a codepoint."""
    if budget <= 0:
        return ""
    enc = text.encode("utf-8")
    if len(enc) <= budget:
        return text
    return enc[:budget].decode("utf-8", errors="ignore")


def _fit_list_prefix(items: list[Any], budget: int) -> list[Any]:
    """Longest leading slice of ``items`` whose serialized form fits ``budget``.

    Keeps whole elements (never a half-serialized record), so the result stays
    valid, parseable JSON. The running total mirrors ``json.dumps`` exactly — the
    enclosing ``[]`` plus a ``", "`` separator between elements — so it neither
    overshoots the budget nor stalls: when ``budget`` is below the full list's
    size (always true when there is overflow to shed) at least one element is
    dropped, guaranteeing forward progress for the caller's loop.
    """
    kept = 0
    used = 2  # the enclosing "[]"
    for i, item in enumerate(items):
        piece = _byte_len(_serialized(item)) + (2 if i else 0)  # ", " between items
        if used + piece > budget:
            break
        used += piece
        kept += 1
    return items[:kept]


def cap_result(result: Any, max_bytes: int) -> Any | None:
    """Return a size-capped copy of ``result``, or ``None`` if already in budget.

    Structure is preserved wherever possible so the model still gets parseable
    JSON, not a truncated (invalid) string:

    * **dict** — each pass trims the single biggest contributor (the longest
      string field, or the longest list field down to its leading records — a
      huge ``rows``/``results`` list is the common Elasticsearch/Kafka shape).
      A small ``status`` the model reads survives.
    * **list** — a top-level list of records (e.g. a big query result) keeps its
      first N elements, wrapped with the kept/total counts, rather than being
      flattened to a truncated JSON string.

    Only when the bloat can't be trimmed structurally does it fall back to a
    wrapped head of the serialized payload. The returned value is always within
    ``max_bytes``. ``max_bytes <= 0`` disables capping (returns ``None``).
    """
    if max_bytes <= 0:
        return None
    serialized = _serialized(result)
    original = _byte_len(serialized)
    if original <= max_bytes:
        return None

    note = (
        f"[output truncated: {original} bytes exceeded the {max_bytes}-byte cap; "
        "re-run with a narrower filter, a shorter time range, or a smaller "
        "limit/tail to see more]"
    )

    if isinstance(result, dict):
        capped = _shrink_dict(dict(result), note, max_bytes)
        if capped is not None:
            return capped

    if isinstance(result, list):
        # Keep the first N records instead of truncating the serialized string
        # into invalid JSON. Reserve room for the wrapper keys + the note.
        overhead = _byte_len(note) + 96
        kept = _fit_list_prefix(result, max(0, max_bytes - overhead))
        if kept:
            wrapped = {
                "status": "truncated",
                "note": note,
                "returned_items": len(kept),
                "total_items": len(result),
                "items": kept,
            }
            if _byte_len(_serialized(wrapped)) <= max_bytes:
                return wrapped
        # else (even one record overflows the budget) → generic fallback below.

    # Generic fallback: wrap the head of the serialized payload, reserving room
    # for the wrapper so the returned dict itself stays within budget.
    overhead = _byte_len(note) + 64
    head = _truncate_to_bytes(serialized, max(0, max_bytes - overhead))
    return {"status": "truncated", "note": note, "output": head}


def _shrink_dict(capped: dict[Any, Any], note: str, max_bytes: int) -> dict[Any, Any] | None:
    """Trim a dict result in place to ``max_bytes``, or ``None`` if it can't.

    Each pass trims the single biggest contributor — the longest string field or
    the longest list field — by the overflow, so a small ``status`` isn't wiped
    just because the real bloat lives in a ``results`` list (and vice versa).
    Both moves preserve the dict's shape. Returns ``None`` when nothing is left
    to trim yet it is still over budget (caller uses the generic fallback).
    """
    capped["_truncated"] = note
    while (over := _byte_len(_serialized(capped)) - max_bytes) > 0:
        str_keys = [k for k, v in capped.items() if k != "_truncated" and isinstance(v, str) and v]
        list_keys = [k for k, v in capped.items() if isinstance(v, list) and v]
        str_key = max(str_keys, key=lambda k: _byte_len(capped[k]), default=None)
        list_key = max(list_keys, key=lambda k: _byte_len(_serialized(capped[k])), default=None)
        str_size = _byte_len(capped[str_key]) if str_key else 0
        list_size = _byte_len(_serialized(capped[list_key])) if list_key else 0
        if str_size == 0 and list_size == 0:
            break  # nothing shrinkable left → generic fallback
        if str_key is not None and str_size >= list_size:
            capped[str_key] = _truncate_to_bytes(capped[str_key], str_size - over)
        else:
            assert list_key is not None
            original_n = len(capped[list_key])
            capped[list_key] = _fit_list_prefix(capped[list_key], max(0, list_size - over))
            if len(capped[list_key]) < original_n:
                capped["_list_truncated"] = (
                    f"{list_key}: kept {len(capped[list_key])} of {original_n} items"
                )
            if not capped[list_key]:
                break  # one record alone overflows → generic fallback
    return capped if _byte_len(_serialized(capped)) <= max_bytes else None


class ToolOutputCapPlugin(BasePlugin):
    """Caps each tool result's size so one huge payload can't break the request."""

    def __init__(self, *, max_bytes: int = DEFAULT_MAX_TOOL_RESULT_BYTES) -> None:
        super().__init__(name="tool_output_cap")
        self._max_bytes = max_bytes

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: Any,
    ) -> Any | None:
        """Replace an oversized tool result with a truncated one; else leave it."""
        capped = cap_result(result, self._max_bytes)
        if capped is not None:
            logger.warning(
                "capped oversized result from tool '%s' (limit %d bytes)",
                tool.name,
                self._max_bytes,
            )
        return capped
