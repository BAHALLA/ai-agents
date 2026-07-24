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

from typing import Any

#: Matches the recursion bound used by the redaction walk.
_MAX_DEPTH = 32


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
