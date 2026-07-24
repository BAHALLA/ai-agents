"""Error callback factories for graceful tool and model failure handling.

Provides on_tool_error_callback and on_model_error_callback factories
that log errors and return user-friendly responses instead of crashing.

Two rules shape what these return, and both are about what the *model* then
does with it:

- **Tool errors keep their detail server-side.** Exception strings from HTTP,
  Kubernetes and Kafka clients routinely embed internal URLs, hosts, and file
  paths; putting them in the model's context leaks them into the transcript and
  onward into whatever the model says next (CWE-209). The full traceback is
  logged privately and only the exception *class* is named — except for an HTTP
  4xx, whose body describes what was wrong with our own request and is what
  lets the model correct and retry.
- **Model errors must read as a failure, not an answer.** When the agent that
  died is a specialist reached through an ``AgentTool``, whatever comes back
  becomes that tool's *result* in the coordinator's transcript — and a polite
  "I hit an error, please try again" reads as a completed step, so the
  coordinator carries on and reports success for work that never ran.

ADK callback signatures:
    on_tool_error:  (tool, args, tool_context, error) -> Optional[dict]
    on_model_error: (callback_context, llm_request, error) -> Optional[LlmResponse]
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.context import Context
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.tools.base_tool import BaseTool
from google.genai import types

logger = logging.getLogger("orrery.errors")

#: How far up the ``__cause__`` chain to look for the real HTTP failure. ADK
#: wraps the provider's error, so the status lives on the cause rather than on
#: what the callback is handed.
_CAUSE_DEPTH = 5


def _is_quota_error(error: BaseException) -> bool:
    """Whether a model failure is a retryable quota exhaustion (HTTP 429).

    Reads the **status code**, not the message: an error string is a display
    artefact that changes between library versions, which would silently turn
    this into "never a quota error". The class name is accepted too, because
    ADK's own resource-exhausted wrapper carries no status of its own. Both
    checks are duck-typed so core stays decoupled from any provider SDK.
    """
    seen: BaseException | None = error
    for _ in range(_CAUSE_DEPTH):
        if seen is None:
            return False
        if any(getattr(seen, attr, None) == 429 for attr in ("code", "status_code")):
            return True
        if "ResourceExhausted" in seen.__class__.__name__:
            return True
        seen = seen.__cause__
    return False


def graceful_tool_error() -> Callable:
    """Create an on_tool_error_callback that returns a structured error dict.

    Instead of crashing the agent, the error is logged and returned as a
    tool result so the LLM can reason about the failure and try alternatives.

    Usage:
        create_agent(
            ...,
            on_tool_error_callback=graceful_tool_error(),
        )
    """

    def callback(
        tool: BaseTool,
        args: dict[str, Any],
        tool_context: Context,
        error: Exception,
    ) -> dict:
        logger.error("Tool '%s' failed with args %s", tool.name, args, exc_info=error)

        # An HTTP 4xx body says what was wrong with *our* request (a bad field,
        # a missing value), so surfacing it lets the model fix the call instead
        # of retrying blind. Everything else — 5xx, connection errors, arbitrary
        # client exceptions — keeps its message in the log only: those strings
        # carry internal hosts, URLs and paths. Detected by duck-typing so this
        # stays decoupled from any specific client library.
        status = getattr(error, "status_code", None) or getattr(error, "status", None)
        detail = getattr(error, "detail", None) or getattr(error, "reason", None)
        if isinstance(status, int) and 400 <= status < 500 and detail:
            message = (
                f"The '{tool.name}' tool was rejected by the service (HTTP {status}): "
                f"{detail}. Fix the request accordingly, then retry — do not repeat "
                f"the same call unchanged."
            )
        else:
            message = (
                f"The '{tool.name}' tool failed with a {type(error).__name__}. "
                f"The details were logged server-side; do not retry blindly."
            )
        return {
            "status": "error",
            "error_type": type(error).__name__,
            "message": message,
        }

    return callback


def graceful_model_error() -> Callable:
    """Create an on_model_error_callback that reports the step as failed.

    Instead of crashing, returns an ``LlmResponse`` that states plainly that
    nothing was produced. The wording is load-bearing rather than cosmetic: in
    this platform the failing agent is usually a specialist invoked as an
    ``AgentTool`` by the chat root or the triage workflow, so this text lands in
    the coordinator's transcript *as that tool's result*. A conversational
    apology is indistinguishable from a finding, and the coordinator will
    happily summarize an incident whose Kafka check never ran — so the response
    says the step failed, that there is no result, and not to build on it.

    Usage:
        create_agent(
            ...,
            on_model_error_callback=graceful_model_error(),
        )
    """

    def callback(
        callback_context: CallbackContext,
        llm_request: LlmRequest,
        error: Exception,
    ) -> LlmResponse:
        logger.error("Model call failed: %s", type(error).__name__, exc_info=error)
        # The raw error can carry the model endpoint, request ids or internal
        # hosts, so it stays in the log; only its class is named here. 429 is
        # called out because it is the common one and genuinely retryable.
        cause = (
            "the model is out of quota right now (429)"
            if _is_quota_error(error)
            else f"the model call failed ({type(error).__name__})"
        )
        return LlmResponse(
            content=types.Content(
                role="model",
                parts=[
                    types.Part.from_text(
                        text=(
                            f"⚠️ STEP FAILED — {cause}. **No result was produced: nothing "
                            "was checked, analysed, or decided here.** Do not treat this "
                            "as an answer and do not continue with any step that depended "
                            "on it (do not remediate, scale, delete, or report success). "
                            "Tell the user this step failed and that it can be retried."
                        )
                    )
                ],
            )
        )

    return callback
