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
from typing import Any

from google.adk.agents import Agent
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.apps import App
from google.adk.memory.base_memory_service import BaseMemoryService
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.runners import Runner
from google.adk.sessions.database_session_service import DatabaseSessionService
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.adk.workflow import Workflow
from google.genai import types as genai_types

try:
    from fastapi import Depends, FastAPI, HTTPException, Request, status
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
    from pydantic import BaseModel, Field
except ImportError as exc:  # pragma: no cover — covered by the install-extra path
    raise ImportError(
        "orrery_core.server requires FastAPI. Install with: uv sync --extra server"
    ) from exc

from .auth import AUTH_STATE_KEY, AuthContext, AuthError, JWTConfig, verify_token
from .events import extract_reply_text
from .log import mask_dsn

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

    if cfg.database_url:
        logger.info("Using database session store: %s", mask_dsn(cfg.database_url))
        session_service: Any = DatabaseSessionService(db_url=cfg.database_url)
    else:
        logger.warning(
            "Using in-memory session store — sessions will be lost on restart "
            "and cannot be shared across replicas."
        )
        session_service = InMemorySessionService()

    adk_app = App(
        name=app_name,
        root_agent=root_agent,
        plugins=list(plugins or []),
        context_cache_config=context_cache_config,
    )
    runner = Runner(app=adk_app, session_service=session_service, memory_service=memory_service)

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
        user_id = auth.subject

        session = None
        if body.session_id:
            session = await session_service.get_session(
                app_name=app_name, user_id=user_id, session_id=body.session_id
            )

        if session is None:
            initial_state: dict[str, Any] = {AUTH_STATE_KEY: auth.as_state()}
            session = await session_service.create_session(
                app_name=app_name, user_id=user_id, state=initial_state
            )
        else:
            # Re-stamp auth context so a long-lived session inherits the
            # latest verified role (the token may have been re-minted with
            # a different role since the session was created).
            session.state[AUTH_STATE_KEY] = auth.as_state()

        message = genai_types.Content(
            role="user",
            parts=[genai_types.Part.from_text(text=body.message)],
        )

        response_text = ""
        async for event in runner.run_async(
            user_id=user_id, session_id=session.id, new_message=message
        ):
            response_text += extract_reply_text(event)

        return ChatResponse(session_id=session.id, response=response_text)

    return api
