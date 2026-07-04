"""Authenticated HTTP front door for the agent runtime.

ADK ships a ``adk web`` dev UI that has no authentication. This module
provides the production replacement: a small FastAPI app that mounts an
ADK ``Runner`` behind a JWT Bearer dependency. Every request must carry
a valid token; the verified ``AuthContext`` is stashed in session state
so :class:`AuthPlugin` can apply the correct role on the first agent
invocation.

Requires the ``orrery-core[server]`` extra (FastAPI + Uvicorn + PyJWT).
Importing this module without the extra installed will raise
``ImportError`` at import time with installation guidance — by design,
since the rest of the package does not depend on this module.

Usage::

    from orrery_core.server import create_app, ServerConfig
    from orrery_core import default_plugins
    from my_agents import root_agent

    config = ServerConfig.from_env()
    app = create_app(
        root_agent=root_agent,
        app_name="orrery",
        plugins=default_plugins(enable_auth=True),
        config=config,
    )
    # uvicorn my_module:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass, field

from google.adk.agents import Agent
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.memory.base_memory_service import BaseMemoryService
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.workflow import Workflow

try:
    from fastapi import Depends, FastAPI, HTTPException, Request, status
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
    from pydantic import BaseModel, Field
except ImportError as exc:  # pragma: no cover — covered by the install-extra path
    raise ImportError(
        "orrery_core.server requires FastAPI. Install with: uv sync --extra server"
    ) from exc

from ..persistence.db import create_session_service
from ..security.auth import AUTH_STATE_KEY, AuthContext, AuthError, JWTConfig, verify_token
from .gateway import AgentGateway, ExplicitSessionResolver, InboundMessage

logger = logging.getLogger("orrery.server")


# ── Config ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ServerConfig:
    """HTTP server configuration loaded from environment variables."""

    auth_enabled: bool = True
    jwt: JWTConfig = field(default_factory=JWTConfig)
    database_url: str | None = None
    cors_origins: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> ServerConfig:
        """Load configuration from environment variables.

        Reads:

        - ``AUTH_ENABLED`` (default ``true``)
        - ``JWT_*`` (see :class:`JWTConfig`)
        - ``DATABASE_URL`` (Postgres recommended for multi-replica)
        - ``ORRERY_CORS_ORIGINS`` (comma-separated)
        """
        cors = os.getenv("ORRERY_CORS_ORIGINS", "")
        return cls(
            auth_enabled=os.getenv("AUTH_ENABLED", "true").lower() in ("1", "true", "yes"),
            jwt=JWTConfig.from_env(),
            database_url=os.getenv("DATABASE_URL") or None,
            cors_origins=tuple(o.strip() for o in cors.split(",") if o.strip()),
        )


# ── Request / response models ───────────────────────────────────────


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8192)
    session_id: str | None = Field(default=None, max_length=128)


class ChatResponse(BaseModel):
    session_id: str
    response: str


# ── App factory ─────────────────────────────────────────────────────


def create_app(
    *,
    root_agent: Agent | Workflow,
    app_name: str,
    plugins: Sequence[BasePlugin] | None = None,
    config: ServerConfig | None = None,
    memory_service: BaseMemoryService | None = None,
    context_cache_config: ContextCacheConfig | None = None,
) -> FastAPI:
    """Build a FastAPI app that serves *root_agent* over authenticated HTTP.

    Endpoints:

    - ``POST /chat`` — body ``{"message": str, "session_id": str | None}``;
      returns ``{"session_id": str, "response": str}``.
    - ``GET /healthz`` — liveness (always 200 once the app is up).
    - ``GET /readyz`` — readiness (200 once the runner is initialised).

    Sessions are persisted to ``config.database_url`` when set (Postgres
    recommended for multi-replica), otherwise an in-memory store is used
    — fine for single-process testing, **not** safe for production scale.
    """
    cfg = config or ServerConfig.from_env()

    # Validate JWT config eagerly when auth is on — fail fast at startup
    # rather than on the first request.
    if cfg.auth_enabled:
        cfg.jwt.validate()
        logger.info("Auth enabled (algorithm=%s)", cfg.jwt.algorithm)
    else:
        logger.warning(
            "Auth DISABLED. Do not run this configuration in production — "
            "RBAC is meaningless without verified identity."
        )

    # The gateway owns the shared turn pipeline (runner, session mapping, run
    # loop, reply extraction). This HTTP surface is one ChannelAdapter over it.
    # Probe the configured database and fall back to in-memory sessions (with a
    # warning) when it is unset or unreachable, rather than crashing at startup.
    gateway = AgentGateway(
        app_name=app_name,
        root_agent=root_agent,
        plugins=list(plugins or []),
        session_service=create_session_service(cfg.database_url),
        memory_service=memory_service,
        context_cache_config=context_cache_config,
        session_resolver=ExplicitSessionResolver(),
    )

    api = FastAPI(title=f"{app_name} (orrery)", docs_url="/docs", redoc_url=None)

    if cfg.cors_origins:
        api.add_middleware(
            CORSMiddleware,
            allow_origins=list(cfg.cors_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
        )

    bearer_scheme = HTTPBearer(auto_error=False)

    async def auth_dependency(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),  # noqa: B008 — FastAPI dependency pattern
    ) -> AuthContext:
        """Resolve the bearer token into an ``AuthContext`` or raise 401."""
        if not cfg.auth_enabled:
            # Anonymous mode — pin to viewer so RBAC still gates writes.
            host = request.client.host if request.client else "unknown"
            return AuthContext(subject=f"anonymous:{host}", role="viewer", claims={})

        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

        try:
            return verify_token(credentials.credentials, cfg.jwt)
        except AuthError as exc:
            logger.info("Auth rejected for client %s: %s", request.client, exc)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc

    @api.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @api.get("/readyz")
    async def readyz() -> dict[str, str]:
        return {"status": "ready"}

    @api.post("/chat", response_model=ChatResponse)
    async def chat(
        body: ChatRequest,
        auth: AuthContext = Depends(auth_dependency),  # noqa: B008 — FastAPI dependency pattern
    ) -> ChatResponse:
        # Identity travels in state_delta each turn (applied before the agent
        # runs), so a long-lived session always inherits the latest verified
        # role and AuthPlugin resolves RBAC from it.
        msg = InboundMessage(
            text=body.message,
            user_id=auth.subject,
            conversation_key=body.session_id or "",
            channel="http",
            state_delta={AUTH_STATE_KEY: auth.as_state()},
        )
        reply = await gateway.run(msg)
        return ChatResponse(session_id=reply.session_id, response=reply.text)

    return api
