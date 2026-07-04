"""ResiliencePlugin — per-tool circuit breaker registered globally."""

from __future__ import annotations

from typing import Any

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from ..reliability.resilience import CircuitBreaker


class ResiliencePlugin(BasePlugin):
    """Circuit breaker that tracks per-tool failures globally.

    Args:
        failure_threshold: Failures before opening the circuit.
        recovery_timeout: Seconds before allowing a probe call.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
    ) -> None:
        super().__init__(name="resilience")
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=failure_threshold,
            recovery_timeout=recovery_timeout,
        )
        self._before = self.circuit_breaker.before_tool_callback()
        self._after = self.circuit_breaker.after_tool_callback()
        self._on_error = self.circuit_breaker.on_tool_error_callback()

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
        return self._after(tool, tool_args, tool_context, result)

    async def on_tool_error_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        error: Exception,
    ) -> dict | None:
        # Record failure but don't suppress — let ErrorHandlerPlugin handle it.
        self._on_error(tool, tool_args, tool_context, error)
        return None
