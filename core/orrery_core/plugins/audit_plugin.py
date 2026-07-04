"""AuditPlugin — structured audit logging for every tool invocation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from ..observability.audit import audit_logger


class AuditPlugin(BasePlugin):
    """Structured audit logging for every tool invocation.

    Args:
        log_path: Optional path to also write a local .jsonl file.
    """

    def __init__(self, log_path: str | Path | None = None) -> None:
        super().__init__(name="audit")
        self._callback = audit_logger(log_path)

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
