"""ActivityPlugin — records tool calls in session state for cross-agent visibility."""

from __future__ import annotations

from typing import Any

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from ..observability.activity import activity_tracker


class ActivityPlugin(BasePlugin):
    """Tracks tool calls in session state for cross-agent visibility."""

    def __init__(self) -> None:
        super().__init__(name="activity")
        self._callback = activity_tracker()

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict,
    ) -> dict | None:
        return self._callback(
            tool=tool, args=tool_args, tool_context=tool_context, tool_response=result
        )
