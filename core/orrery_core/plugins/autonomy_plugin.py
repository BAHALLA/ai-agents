"""AutonomyPlugin — graduated SRE autonomy levels (L2/L3/L4).

A secure, asymmetric guardrail over tool execution, orthogonal to RBAC: RBAC
answers *who* may act, autonomy answers *which mode this process runs in*. It
lets the same agent be deployed read-only for a new audience without touching
roles:

  - **L2 (read-only)** — fail-closed: only read tools or explicitly
    whitelisted tools run; every mutation is blocked.
  - **L3 (mutating SRE)** — fail-open for mutations except the irreversible,
    high-blast-radius ones (``@destructive`` / explicit blacklist), which are
    blocked.
  - **L4 (confirmed mutating)** — destructive tools are allowed only after an
    explicit human-in-the-loop confirmation (ADK-native
    ``request_confirmation``; supported by ``adk web``).

Read/mutate/destructive classification comes from the guardrail decorators —
the same source of truth RBAC uses: unmarked tools are read, ``@confirm`` marks
a mutation, ``@destructive`` marks an irreversible one. So the gate matches the
repo's real tool metadata instead of guessing from verb prefixes.

Blocked calls **return a structured deny result** rather than raising, so the
attempt flows through the rest of the plugin chain (metrics) and the model
receives a clean, actionable message instead of an unhandled exception.

Opt-in: registered by ``default_plugins()`` only when an autonomy level is
configured (``autonomy_level=`` argument or the ``ORRERY_AUTONOMY_LEVEL`` env
var), so existing deployments are unchanged.
"""

from __future__ import annotations

import logging
from typing import Any

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from ..security.guardrails import get_guard_level, is_destructive

logger = logging.getLogger("orrery.plugins")

# Session-state key an upstream component (transport/server) may set to
# override the configured level per request.
AUTONOMY_LEVEL_STATE_KEY = "autonomy_level"

_VALID_LEVELS = frozenset({"L2", "L3", "L4"})


def _deny(tool_name: str, level: str, reason: str) -> dict[str, Any]:
    """Build the structured deny result returned in place of a tool call."""
    return {
        "status": "BLOCKED",
        "tool": tool_name,
        "autonomy_level": level,
        "message": f"Tool '{tool_name}' blocked at autonomy level '{level}': {reason}",
    }


class AutonomyPlugin(BasePlugin):
    """ADK autonomy enforcer — read-only (L2), mutating (L3), confirmed (L4)."""

    def __init__(
        self,
        *,
        level: str = "L2",
        l2_whitelist: list[str] | None = None,
        l3_blacklist: list[str] | None = None,
    ) -> None:
        super().__init__(name="autonomy")
        self._level = self._normalise(level)
        # Explicit overrides on top of the decorator-based classification.
        self._l2_whitelist = set(l2_whitelist or [])
        self._l3_blacklist = set(l3_blacklist or [])

    @staticmethod
    def _normalise(level: str) -> str:
        lvl = (level or "L2").upper()
        return lvl if lvl in _VALID_LEVELS else "L2"

    def _allowed_at_l2(self, tool: BaseTool) -> bool:
        # Unmarked tools are read-only by convention (same inference RBAC uses).
        return tool.name in self._l2_whitelist or get_guard_level(tool) is None

    def _blocked_at_l3(self, tool: BaseTool) -> bool:
        return tool.name in self._l3_blacklist or is_destructive(tool)

    def _active_level(self, tool_context: ToolContext) -> str:
        state = getattr(tool_context, "state", None)
        get = getattr(state, "get", None)
        if callable(get) and get(AUTONOMY_LEVEL_STATE_KEY):
            return self._normalise(get(AUTONOMY_LEVEL_STATE_KEY))
        return self._level

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> dict[str, Any] | None:
        """Allow, block (deny result), or request confirmation per active level."""
        level = self._active_level(tool_context)
        name = tool.name

        if level == "L2":
            if not self._allowed_at_l2(tool):
                logger.warning("autonomy L2 blocked mutating tool: %s", name)
                return _deny(name, level, "read-only level forbids mutating tools")
            return None

        if level == "L3":
            if self._blocked_at_l3(tool):
                logger.warning("autonomy L3 blocked destructive tool: %s", name)
                return _deny(name, level, "destructive tool requires L4 confirmation")
            return None

        # L4 — destructive tools require explicit user confirmation (HITL).
        if self._blocked_at_l3(tool):
            confirmation = getattr(tool_context, "tool_confirmation", None)
            if confirmation is None:
                tool_context.request_confirmation(
                    hint=(
                        f"SRE safety check: confirm executing destructive tool "
                        f"'{name}' with args {tool_args}?"
                    )
                )
                # Pause this call until the user answers; ADK re-invokes on reply.
                return _deny(name, level, "awaiting user confirmation")
            if not confirmation.confirmed:
                logger.warning("autonomy L4 user rejected destructive tool: %s", name)
                return _deny(name, level, "user rejected the confirmation")
        return None
