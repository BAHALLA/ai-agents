"""Automatic session activity tracking via after_tool_callback.

Logs every tool execution to session state under the ``session_log`` key,
making the activity visible to ``get_session_summary()`` in the journal agent
regardless of which sub-agent performed the work.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from google.adk.agents.context import Context
from google.adk.tools.base_tool import BaseTool


def activity_tracker() -> Callable:
    """Create an after_tool_callback that appends every tool call to session_log.

    The log entries are stored in ``ctx.state["session_log"]`` using the same
    format as ``ops_journal_agent.tools.log_operation`` so that
    ``get_session_summary`` picks them up automatically.

    Usage:
        create_agent(
            ...,
            after_tool_callback=activity_tracker(),
        )
    """

    def callback(
        *,
        tool: BaseTool,
        args: dict[str, Any],
        tool_context: Context,
        tool_response: dict,
    ) -> dict | None:
        status = tool_response.get("status", "ok") if isinstance(tool_response, dict) else "ok"
        agent_name = tool_context.agent_name if hasattr(tool_context, "agent_name") else "unknown"

        entry = {
            "operation": tool.name,
            "details": f"[{agent_name}] {_summarize_args(args)} → {status}",
            "timestamp": datetime.now(UTC).isoformat(),
        }

        log = tool_context.state.get("session_log", [])
        log.append(entry)
        tool_context.state["session_log"] = log

        return None  # don't modify the tool response

    return callback


def _summarize_args(args: dict[str, Any]) -> str:
    """Build a compact one-line summary of tool arguments."""
    if not args:
        return "(no args)"
    parts = [f"{k}={v}" for k, v in args.items() if k != "ctx"]
    return ", ".join(parts) if parts else "(no args)"
