"""ErrorHandlerPlugin — graceful recovery for tool and model failures."""

from __future__ import annotations

from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from ..reliability.error_handlers import graceful_model_error, graceful_tool_error


class ErrorHandlerPlugin(BasePlugin):
    """Graceful error recovery for tool and model failures.

    Must be registered **last** so other plugins can observe the error
    before this one suppresses it with a structured response.
    """

    def __init__(self) -> None:
        super().__init__(name="error_handler")
        self._tool_error = graceful_tool_error()
        self._model_error = graceful_model_error()

    async def on_tool_error_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        error: Exception,
    ) -> dict | None:
        return self._tool_error(tool, tool_args, tool_context, error)

    async def on_model_error_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
        error: Exception,
    ) -> LlmResponse | None:
        return self._model_error(callback_context, llm_request, error)
