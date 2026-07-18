"""Reusable persistent runner for CLI-based agent interaction.

Wraps the ADK Runner with DatabaseSessionService so that session state,
user notes, and app-wide data survive across restarts.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Sequence

from google.adk.agents import Agent
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.memory.base_memory_service import BaseMemoryService
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.workflow import Workflow

from ..observability.log import mask_dsn
from ..persistence.db import create_session_service
from ..security.rbac import set_user_role
from .gateway import AgentGateway
from .health import HealthServer

logger = logging.getLogger("orrery.runner")


def create_context_cache_config(
    *,
    min_tokens: int | None = None,
    ttl_seconds: int | None = None,
    cache_intervals: int | None = None,
) -> ContextCacheConfig:
    """Create a ``ContextCacheConfig`` with env-var defaults.

    Each parameter falls back to an environment variable, then to ADK defaults:

    - ``CONTEXT_CACHE_MIN_LENGTH`` (default: 2048)
    - ``CONTEXT_CACHE_TTL_SECONDS`` (default: 600)
    - ``CONTEXT_CACHE_INTERVALS`` (default: 10)

    Note: context caching is only supported for Gemini models.  When using
    Claude/OpenAI via LiteLLM, the config is accepted but has no effect.
    """
    resolved_min_tokens = (
        min_tokens if min_tokens is not None else int(os.getenv("CONTEXT_CACHE_MIN_LENGTH", "2048"))
    )
    resolved_ttl = (
        ttl_seconds
        if ttl_seconds is not None
        else int(os.getenv("CONTEXT_CACHE_TTL_SECONDS", "600"))
    )
    resolved_intervals = (
        cache_intervals
        if cache_intervals is not None
        else int(os.getenv("CONTEXT_CACHE_INTERVALS", "10"))
    )
    return ContextCacheConfig(
        min_tokens=resolved_min_tokens,
        ttl_seconds=resolved_ttl,
        cache_intervals=resolved_intervals,
    )


async def run_persistent(
    agent: Agent | Workflow,
    *,
    app_name: str,
    db_url: str | None = None,
    user_id: str = "default_user",
    plugins: Sequence[BasePlugin] | None = None,
    memory_service: BaseMemoryService | None = None,
    health_port: int | None = None,
    context_cache_config: ContextCacheConfig | None = None,
) -> None:
    """Run an agent in a persistent CLI loop backed by in-memory or PostgreSQL.

    Args:
        agent: The root agent to run.
        app_name: Application name for session scoping.
        db_url: PostgreSQL URL. When omitted, falls back to ``DATABASE_URL`` and
            then to an in-memory session store.
        user_id: User ID for session scoping.
        plugins: Optional list of ADK plugins for cross-cutting concerns.
            Use ``default_plugins()`` for the standard set.
        memory_service: Optional memory service for cross-session recall.
            When omitted, a redacting memory service co-located with the session
            store is created automatically (see ``create_memory_service``), so
            long-term recall persists when PostgreSQL is configured.
        health_port: Port for the health probe server.  Defaults to the
            ``HEALTH_PORT`` env var or 8080.
        context_cache_config: Optional context caching configuration.
            Use ``create_context_cache_config()`` for env-var-configurable
            defaults.  Only effective with Gemini models.

    Session store resolution order:

    1. Explicit ``db_url`` argument (highest priority).
    2. ``DATABASE_URL`` environment variable — a PostgreSQL URL
       (``postgresql+asyncpg://user:pass@host:5432/agents``). Required for
       multi-replica deployments.
    3. No URL → an in-memory session store (single process, lost on restart).
    """
    resolved_db_url = db_url or os.getenv("DATABASE_URL")
    # Probe first; fall back to an in-memory session store (with a warning) if
    # PostgreSQL is configured but unreachable, instead of crashing on startup.
    session_service = create_session_service(resolved_db_url)

    # Co-locate long-term memory with the session store so recall persists too
    # when PostgreSQL is configured. Callers can still pass an explicit override.
    if memory_service is None:
        from ..persistence.memory import create_memory_service

        memory_service = create_memory_service(db_url=resolved_db_url)

    # The gateway owns the shared turn pipeline; this CLI is one surface over it.
    if context_cache_config is not None:
        logger.info("Context caching enabled: %s", context_cache_config)
    gateway = AgentGateway(
        app_name=app_name,
        root_agent=agent,
        plugins=plugins,
        session_service=session_service,
        memory_service=memory_service,
        context_cache_config=context_cache_config,
        # Guarded tools need an explicit 'approve'/'deny' from the same
        # verified user who triggered them (requester-verified confirmation).
        verified_confirmation=True,
    )

    # Start health probe server
    health = HealthServer()
    health.start(port=health_port)

    # Graceful shutdown via SIGTERM/SIGINT
    shutdown_event = asyncio.Event()

    def _signal_handler(sig: int, _frame: object) -> None:
        sig_name = signal.Signals(sig).name
        logger.info("Received %s, shutting down gracefully...", sig_name)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # CLI user gets admin (local dev). Identity travels per-turn via state_delta.
    admin_delta: dict[str, object] = {}
    set_user_role(admin_delta, "admin")
    session = await session_service.create_session(app_name=app_name, user_id=user_id)

    print(f"{agent.name} (persistent mode)")
    print(f"Session: {session.id}")
    print(f"Store: {mask_dsn(resolved_db_url) if resolved_db_url else 'in-memory'}")
    print("Type 'quit' to exit, 'new' for a new session.\n")

    while not shutdown_event.is_set():
        try:
            user_input = await asyncio.to_thread(input, "You: ")
            user_input = user_input.strip()
        except EOFError, KeyboardInterrupt:
            break

        if shutdown_event.is_set():
            break

        if not user_input:
            continue
        if user_input.lower() == "quit":
            break
        if user_input.lower() == "new":
            session = await session_service.create_session(app_name=app_name, user_id=user_id)
            print(f"\n--- New session: {session.id} ---\n")
            continue

        reply = await gateway.run_in_session(
            user_id=user_id,
            session_id=session.id,
            text=user_input,
            state_delta=admin_delta,
        )
        response_text = reply.text

        if response_text:
            print(f"\nAgent: {response_text}\n")

    logger.info("Shutdown complete.")
