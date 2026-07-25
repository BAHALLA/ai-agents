"""Cheap size measurement for tool results.

Several plugins need to know how large a tool result is before deciding what
to do with it — whether to offload redaction to a thread, whether a result is
too big to put in an audit line. The obvious way to find out is
``len(json.dumps(result))``, but that costs as much as the work being decided
about (~46 ms on a 5 MiB payload) and allocates a full second copy.

:func:`text_volume` walks the structure instead and sums string lengths, which
is O(number of nodes) rather than O(bytes) — ``len(str)`` is a constant-time
attribute read, so a 20 MiB log blob in one field costs a single addition. It
is an approximation of the serialized size (it ignores JSON punctuation and
number formatting), which is all a size *threshold* ever needs.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

#: Matches the recursion bound used by the redaction walk.
_MAX_DEPTH = 32

#: Above this many characters of text, a scanning after-tool plugin should move
#: its work to a worker thread.
#:
#: Those callbacks are ``async`` but do pure CPU work (several regex passes over
#: the same text), so on a big payload one of them holds the event loop and every
#: other in-flight request stalls with it — a 20 MiB ``get_pod_logs`` result
#: measured ~1.3 s of blocking for redaction alone. Below the threshold the
#: common case (small status dicts) stays inline, where a thread hop would cost
#: more than the scan itself.
#:
#: Shared by :mod:`orrery_core.plugins.pii_plugin` and
#: :mod:`orrery_core.plugins.safety_plugin` so both make the same call about the
#: same payload. Note these plugins see the *uncapped* result:
#: ``ToolOutputCapPlugin`` must stay last in the chain (it returns a replacement,
#: which early-exits ADK's after-tool chain), so its 4 MiB cap never bounds what
#: arrives here.
OFFLOAD_THRESHOLD_CHARS = 256 * 1024


def text_volume(obj: Any, limit: int | None = None) -> int:
    """Total characters held in the strings of *obj* (keys included).

    Args:
        obj: Any tool result — dict, list, string, or scalar.
        limit: Stop and return early once the running total reaches this.
            Use it when the answer only has to settle a threshold comparison;
            omit it when the true size is wanted (still cheap).

    Returns:
        The character total, or a value ``>= limit`` if the walk short-circuited.
    """
    total = 0
    stack: list[tuple[Any, int]] = [(obj, 0)]
    while stack:
        item, depth = stack.pop()
        if depth > _MAX_DEPTH:
            continue
        if isinstance(item, str):
            total += len(item)
        elif isinstance(item, dict):
            for key, value in item.items():
                if isinstance(key, str):
                    total += len(key)
                stack.append((value, depth + 1))
        elif isinstance(item, (list, tuple, set, frozenset)):
            for value in item:
                stack.append((value, depth + 1))
        # Numbers, None, and anything else serialize to a handful of bytes.
        if limit is not None and total >= limit:
            return total
    return total


def mutable_attributes(obj: Any) -> dict[str, Any] | None:
    """The writable attribute dict of a non-container object, or ``None``.

    Tool results are usually dicts, but not always: ADK's own ``load_memory``
    returns a Pydantic ``LoadMemoryResponse``, and a dict/list-only walk skipped it
    entirely — so recalled memories reached the model and the audit log with no
    redaction and no injection screening at all. Pydantic models are mutable by
    default, so their fields can be rewritten in place like any other attribute
    holder, which keeps the return-``None`` contract intact.

    Excludes strings, bytes and containers (handled by the caller's own branches)
    and anything without a ``__dict__`` — a scalar has nothing to walk into.
    """
    if isinstance(obj, (str, bytes, dict, list, tuple, set, frozenset)):
        return None
    if isinstance(obj, (int, float, complex, bool, type(None))):
        return None
    attributes = getattr(obj, "__dict__", None)
    return attributes if isinstance(attributes, dict) and attributes else None


def map_strings(obj: Any, transform: Callable[[str], tuple[str, int]], _depth: int = 0) -> int:
    """Rewrite every string in *obj* **in place** via *transform*; return the count.

    *transform* takes a string and returns ``(new_string, n)`` where ``n`` counts
    the substitutions it made; a zero count leaves the original object untouched
    (no needless writes).

    Walks dicts, lists, and the attributes of ordinary objects (see
    :func:`mutable_attributes`, which is what lets a Pydantic tool result such as
    ``LoadMemoryResponse`` be scrubbed). Strings nested in tuples/sets are left
    alone: rebuilding an immutable container would change object identity, and a
    caller cannot mutate a bare string at all — the plugin layer handles that case
    by *returning* a replacement instead.

    In-place is the contract that matters for after-tool plugins: ADK's after-tool
    chain early-exits on the first non-None return, so a plugin that wants later
    observers to see its edits must mutate the shared result and return ``None``.

    Args:
        obj: Any tool result — dict, list, object, or scalar.
        transform: String rewriter returning ``(new_value, substitutions)``.

    Returns:
        Total substitutions reported by *transform* across the structure.
    """
    if _depth > _MAX_DEPTH:
        return 0
    count = 0
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, str):
                new, n = transform(value)
                if n:
                    obj[key] = new
                    count += n
            else:
                count += map_strings(value, transform, _depth + 1)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, str):
                new, n = transform(item)
                if n:
                    obj[i] = new
                    count += n
            else:
                count += map_strings(item, transform, _depth + 1)
    elif (attributes := mutable_attributes(obj)) is not None:
        for name, value in list(attributes.items()):
            if isinstance(value, str):
                new, n = transform(value)
                if n:
                    # setattr, not the __dict__ entry: a Pydantic model validates
                    # and tracks assignment, and bypassing it can desync the model.
                    setattr(obj, name, new)
                    count += n
            else:
                count += map_strings(value, transform, _depth + 1)
    return count
