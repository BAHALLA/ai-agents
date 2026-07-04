"""ADK Plugins for cross-cutting concerns.

Wraps the existing callback factories (metrics, audit, activity tracking,
RBAC, guardrails, resilience, error handling) as ADK Plugins that can be
registered once on the Runner and apply globally to every agent, tool,
and LLM call. Each plugin lives in its own module; this package assembles
them into the correct registration order via :func:`default_plugins`.

Usage::

    from google.adk.apps import App
    from google.adk.runners import Runner
    from orrery_core.plugins import default_plugins

    app = App(name="myapp", root_agent=root_agent, plugins=default_plugins())
    runner = Runner(app=app, session_service=session_service)

Plugin execution order matters — ``default_plugins()`` returns them in the
correct sequence:

1. GuardrailsPlugin  (RBAC — blocks unauthorized calls)
2. ResiliencePlugin   (circuit breaker — blocks calls to failing tools)
3. MetricsPlugin      (timing + counters)
4. AuditPlugin        (structured audit logging)
5. ActivityPlugin     (session activity tracking)
6. MemoryPlugin       (cross-session memory persistence)
7. ErrorHandlerPlugin (graceful error recovery — must be last)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from google.adk.plugins.base_plugin import BasePlugin

from ..security.auth import AuthPlugin
from ..security.rbac import RolePolicy
from .activity_plugin import ActivityPlugin
from .audit_plugin import AuditPlugin
from .error_handler_plugin import ErrorHandlerPlugin
from .guardrails_plugin import GuardrailsPlugin
from .memory_plugin import MemoryPlugin
from .metrics_plugin import MetricsPlugin
from .resilience_plugin import ResiliencePlugin

__all__ = [
    "ActivityPlugin",
    "AuditPlugin",
    "AuthPlugin",
    "ErrorHandlerPlugin",
    "GuardrailsPlugin",
    "MemoryPlugin",
    "MetricsPlugin",
    "ResiliencePlugin",
    "default_plugins",
]

logger = logging.getLogger("orrery.plugins")


def default_plugins(
    *,
    role_policy: RolePolicy | None = None,
    guardrail_mode: str = "confirm",
    circuit_breaker_threshold: int = 5,
    circuit_breaker_timeout: float = 60.0,
    audit_log_path: str | Path | None = None,
    enable_activity_tracking: bool = True,
    enable_memory: bool = False,
    memory_min_events: int = 4,
    enable_auth: bool = False,
    require_auth: bool = True,
    enable_tracing: bool | None = None,
) -> list[BasePlugin]:
    """Create the standard set of cross-cutting plugins.

    Returns plugins in the correct registration order:

    1. TracingPlugin      — OpenTelemetry span enrichment (optional, must run first)
    2. AuthPlugin         — applies verified JWT role (optional)
    3. GuardrailsPlugin   — RBAC + confirmation
    4. ResiliencePlugin   — circuit breaker
    5. MetricsPlugin      — Prometheus metrics (wired to circuit breaker)
    6. AuditPlugin        — structured audit logs
    7. ActivityPlugin     — session activity tracking (optional)
    8. MemoryPlugin       — cross-session memory persistence (optional)
    9. ErrorHandlerPlugin — graceful error recovery

    Args:
        role_policy: Custom ``RolePolicy`` for RBAC overrides.
        guardrail_mode: ``"confirm"``, ``"dry_run"``, or ``"none"``.
        circuit_breaker_threshold: Failures before circuit opens.
        circuit_breaker_timeout: Recovery timeout in seconds.
        audit_log_path: Optional local audit log file path.
        enable_activity_tracking: Whether to track activity in session state.
        enable_memory: Whether to auto-save sessions to long-term memory.
        memory_min_events: Minimum events before a session is saved to memory.
        enable_auth: Whether to apply ``AuthPlugin``. Pair with an HTTP front
            door (see :mod:`orrery_core.serving.server`) that writes the verified
            ``_auth`` payload to session state.
        require_auth: When ``enable_auth=True``, force ``viewer`` if no
            ``_auth`` payload is present. Set ``False`` only for migration
            windows where some transports have not been wired yet.
        enable_tracing: Whether to configure OpenTelemetry and prepend
            ``TracingPlugin``. Defaults to ``None``, which resolves from the
            ``OTEL_TRACING_ENABLED`` env var — so every transport picks up
            tracing from a single env flag with no per-agent wiring. Requires
            the ``orrery-core[otel]`` extra; if the flag is on but the extra
            is missing, tracing is skipped with a warning rather than crashing.
    """
    if enable_tracing is None:
        enable_tracing = os.getenv("OTEL_TRACING_ENABLED", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    resilience = ResiliencePlugin(
        failure_threshold=circuit_breaker_threshold,
        recovery_timeout=circuit_breaker_timeout,
    )

    plugins: list[BasePlugin] = []

    if enable_tracing:
        # Imported lazily: tracing.py imports OpenTelemetry at module load, so
        # only touch it when opted in (mirrors the [otel] extra). A missing
        # extra is a skip-with-warning, not a crash.
        try:
            from ..observability.tracing import TracingPlugin, configure_tracing
        except ImportError:
            logger.warning(
                "OTEL_TRACING_ENABLED is set but the [otel] extra is not installed; "
                "skipping tracing. Install with: uv sync --extra otel"
            )
        else:
            if configure_tracing():
                plugins.append(TracingPlugin())

    if enable_auth:
        plugins.append(AuthPlugin(require_auth=require_auth))

    plugins.extend(
        [
            GuardrailsPlugin(role_policy=role_policy, mode=guardrail_mode),
            resilience,
            MetricsPlugin(circuit_breaker=resilience.circuit_breaker),
            AuditPlugin(log_path=audit_log_path),
        ]
    )

    if enable_activity_tracking:
        plugins.append(ActivityPlugin())

    if enable_memory:
        plugins.append(MemoryPlugin(min_events=memory_min_events))

    plugins.append(ErrorHandlerPlugin())

    return plugins
