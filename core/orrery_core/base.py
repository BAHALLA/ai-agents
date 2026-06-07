from __future__ import annotations

import logging
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.agents.base_agent import BaseAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.planners import BasePlanner
from google.adk.tools.base_tool import BaseTool

from .log import setup_logging

logger = logging.getLogger("orrery.base")


def load_agent_env(agent_file: str | None = None) -> None:
    """Load the .env file for the agent.

    By default, it searches for a .env file in the current working directory
    and its parents (centralized configuration). If ``agent_file`` is provided
    and a .env file exists in the same directory, it is loaded with precedence.

    Also configures structured JSON logging to stdout on first call.

    Usage in an agent's agent.py:
        load_agent_env(__file__)
    """
    # 1. Load centralized .env (searches CWD and parents)
    load_dotenv()

    # 2. Fallback/Override: local .env next to the agent module
    if agent_file:
        local_env = Path(agent_file).parent / ".env"
        if local_env.exists():
            load_dotenv(dotenv_path=local_env, override=True)

    setup_logging()


def resolve_model() -> str | BaseLlm:
    """Resolve the LLM model from environment variables.

    Reads MODEL_PROVIDER and MODEL_NAME to determine which backend to use.
    For Gemini (the default), returns a plain model string.
    For other providers (anthropic, openai, ollama, etc.), returns a LiteLlm instance.

    Environment variables:
        MODEL_PROVIDER: "gemini" (default), "anthropic", "openai", "ollama", etc.
        MODEL_NAME: Model identifier (e.g., "gemini-2.5-pro", "anthropic/claude-sonnet-4-20250514").
        GEMINI_MODEL_VERSION: Legacy alias for MODEL_NAME when provider is gemini.

    Returns:
        A model string for Gemini or a LiteLlm instance for other providers.
    """
    provider = os.getenv("MODEL_PROVIDER", "gemini").lower()

    if provider == "gemini":
        return os.getenv("MODEL_NAME") or os.getenv("GEMINI_MODEL_VERSION") or "gemini-2.0-flash"

    # Non-Gemini provider — use LiteLlm
    from google.adk.models.lite_llm import LiteLlm

    model_name = os.getenv("MODEL_NAME", "")
    if not model_name:
        raise ValueError(
            f"MODEL_NAME must be set when MODEL_PROVIDER={provider}. "
            f"Example: MODEL_NAME=anthropic/claude-sonnet-4-20250514"
        )

    # LiteLlm expects the provider prefix in the model name (e.g., "anthropic/claude-...")
    # Add it if not already present.
    if "/" not in model_name:
        model_name = f"{provider}/{model_name}"

    logger.info("Using LiteLlm with model: %s", model_name)
    return LiteLlm(model=model_name)


def resolve_planner() -> BasePlanner | None:
    """Resolve an ADK planner from environment variables.

    Read ``ORRERY_PLANNER`` to select the planner attached to opted-in agents
    (typically the root orchestrator). Planning adds an explicit reasoning
    step before tool calls — useful for orchestration agents that route
    across multiple specialists, less useful for narrow tool-leaf agents.

    Values:
        ``none`` (default): no planner.
        ``plan_react``: ``PlanReActPlanner`` — provider-agnostic; structures
            the model output into PLANNING / ACTION / REASONING / FINAL_ANSWER
            phases. Works with all providers supported by ``resolve_model``.
        ``builtin``: ``BuiltInPlanner`` — uses Gemini's native thinking
            tokens. Falls back to ``None`` with a warning when
            ``MODEL_PROVIDER`` is not ``gemini``, since LiteLLM-routed models
            do not consume the ADK thinking config.

    Additional env vars (only consulted when ``ORRERY_PLANNER=builtin``):
        ``ORRERY_PLANNER_THINKING_BUDGET``: int token budget for thinking.
        ``ORRERY_PLANNER_INCLUDE_THOUGHTS``: bool (default ``true``) — include
            the model's thoughts in the response stream.

    Returns:
        A ``BasePlanner`` instance or ``None`` to skip planning.
    """
    choice = os.getenv("ORRERY_PLANNER", "none").lower().strip()

    if choice in ("", "none"):
        return None

    if choice == "plan_react":
        from google.adk.planners import PlanReActPlanner

        logger.info("Using PlanReActPlanner")
        return PlanReActPlanner()

    if choice == "builtin":
        provider = os.getenv("MODEL_PROVIDER", "gemini").lower()
        if provider != "gemini":
            logger.warning(
                "ORRERY_PLANNER=builtin requires MODEL_PROVIDER=gemini "
                "(current: %s); falling back to no planner.",
                provider,
            )
            return None

        from google.adk.planners import BuiltInPlanner
        from google.genai import types

        thinking_kwargs: dict[str, Any] = {}
        if budget := os.getenv("ORRERY_PLANNER_THINKING_BUDGET"):
            thinking_kwargs["thinking_budget"] = int(budget)
        include = os.getenv("ORRERY_PLANNER_INCLUDE_THOUGHTS", "true").lower()
        thinking_kwargs["include_thoughts"] = include in ("1", "true", "yes")

        logger.info("Using BuiltInPlanner with thinking_config=%s", thinking_kwargs)
        return BuiltInPlanner(thinking_config=types.ThinkingConfig(**thinking_kwargs))

    logger.warning("Unknown ORRERY_PLANNER value %r; using no planner.", choice)
    return None


def create_agent(
    *,
    name: str,
    description: str,
    instruction: str,
    tools: Sequence[Callable[..., Any] | BaseTool],
    model: str | BaseLlm | None = None,
    planner: BasePlanner | None = None,
    sub_agents: Sequence[BaseAgent] | None = None,
    before_tool_callback: Callable | list[Callable] | None = None,
    after_tool_callback: Callable | list[Callable] | None = None,
    on_tool_error_callback: Callable | list[Callable] | None = None,
    on_model_error_callback: Callable | list[Callable] | None = None,
    output_key: str | None = None,
) -> Agent:
    """Create an ADK Agent with sensible defaults.

    The model is resolved from environment variables via resolve_model()
    unless explicitly passed. Supports Gemini (default), Claude, OpenAI,
    and any LiteLLM-compatible provider.

    Args:
        model: Explicit model override. Can be a Gemini model string or a
            BaseLlm instance (e.g., LiteLlm). When None, resolved from env.
        planner: Optional ADK planner attached to this agent (e.g.,
            ``PlanReActPlanner`` or ``BuiltInPlanner``). Use
            ``resolve_planner()`` to read the choice from env. Reserve this
            for orchestration agents — tool-leaf agents do not benefit.
        before_tool_callback: Called before each tool execution. Return a dict
            to skip the tool (e.g., for guardrails), or None to proceed.
            Use guardrails.require_confirmation() or guardrails.dry_run().
        after_tool_callback: Called after each tool execution. Return a dict
            to override the result, or None to keep it.
            Use audit.audit_logger() for structured logging.
        on_tool_error_callback: Called when a tool raises an exception.
            Return a dict to use as the tool result (graceful recovery),
            or None to let the error propagate.
            Use error_handlers.graceful_tool_error().
        on_model_error_callback: Called when the model call fails.
            Return an LlmResponse to use instead, or None to propagate.
            Use error_handlers.graceful_model_error().
        output_key: Session state key to store this agent's output.
            Useful for passing results between agents in a SequentialAgent.
    """
    resolved_model = model if model is not None else resolve_model()

    kwargs: dict[str, Any] = {
        "name": name,
        "model": resolved_model,
        "description": description,
        "instruction": instruction,
        "tools": list(tools),
    }

    if planner is not None:
        kwargs["planner"] = planner
    if sub_agents:
        kwargs["sub_agents"] = list(sub_agents)
    if before_tool_callback:
        kwargs["before_tool_callback"] = before_tool_callback
    if after_tool_callback:
        kwargs["after_tool_callback"] = after_tool_callback
    if on_tool_error_callback:
        kwargs["on_tool_error_callback"] = on_tool_error_callback
    if on_model_error_callback:
        kwargs["on_model_error_callback"] = on_model_error_callback
    if output_key:
        kwargs["output_key"] = output_key

    return Agent(**kwargs)
