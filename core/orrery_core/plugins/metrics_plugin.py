"""MetricsPlugin — Prometheus tool-call metrics registered globally."""

from __future__ import annotations

from typing import Any

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from ..observability.metrics import MetricsCollector
from ..reliability.resilience import CircuitBreaker


class MetricsPlugin(BasePlugin):
    """Prometheus metrics for tool calls, durations, and errors.

    Args:
        circuit_breaker: Optional ``CircuitBreaker`` whose state is exported
            as a Prometheus gauge.
    """

    def __init__(self, circuit_breaker: CircuitBreaker | None = None) -> None:
        super().__init__(name="metrics")
        self._collector = MetricsCollector(circuit_breaker=circuit_breaker)
        self._before = self._collector.before_tool_callback()
        self._after = self._collector.after_tool_callback()
        self._on_error = self._collector.on_tool_error_callback()

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> dict | None:
        return self._before(tool, tool_args, tool_context)

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict,
    ) -> dict | None:
        return self._after(
            tool=tool,
            args=tool_args,
            tool_context=tool_context,
            tool_response=result,
        )

    async def on_tool_error_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        error: Exception,
    ) -> dict | None:
        # Record error metrics but don't suppress.
        self._on_error(tool, tool_args, tool_context, error)
        return None

    def start_server(self, port: int | None = None) -> None:
        """Start the Prometheus HTTP metrics server."""
        self._collector.start_server(port)
