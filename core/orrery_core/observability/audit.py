"""Structured audit logging for tool calls.

Provides callback factories that log tool invocations as structured JSON via
Python's logging module. In containerised environments this goes to stdout
(when ``setup_logging()`` is configured); for local dev an optional file
fallback is available.

Two events per call: :func:`attempt_logger` (before_tool) records the attempt
*before* execution — so a call that crashes mid-tool, or is blocked by a gate
further down the chain, still leaves a record — and :func:`audit_logger`
(after_tool) records the outcome. A gate may short-circuit the call with a
deny dict; that dict flows through the after-tool chain as the result, so its
``status`` (``access_denied``, ``confirmation_required``, …) is audited too.

ADK calls after_tool_callback with keyword args:
    callback(tool=..., args=..., tool_context=..., tool_response=...)
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from google.adk.agents.context import Context
from google.adk.tools.base_tool import BaseTool

logger = logging.getLogger("orrery.audit")


def _resolve_log_path(log_path: str | Path | None) -> Path | None:
    if log_path is None:
        return None
    resolved = Path(log_path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def _write_file_entry(resolved_path: Path | None, entry: dict[str, Any]) -> None:
    if resolved_path is None:
        return
    try:
        with open(resolved_path, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except OSError as e:
        logger.warning("Failed to write audit log file: %s", e)


def attempt_logger(log_path: str | Path | None = None) -> Callable:
    """Create a before_tool_callback that records every tool call *attempt*.

    Emitted before execution and before any downstream gate can block the
    call, so the audit trail always shows what was attempted — even when the
    tool later crashes or a guardrail denies it. Always returns ``None`` so
    the rest of the callback chain (gates, resilience) still runs.

    Args:
        log_path: Optional path to *also* write a local .jsonl file.
    """
    resolved_path = _resolve_log_path(log_path)

    def callback(*, tool: BaseTool, args: dict[str, Any], tool_context: Context) -> None:
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": "tool_attempt",
            "agent": tool_context.agent_name if hasattr(tool_context, "agent_name") else "unknown",
            "tool": tool.name,
            "args": _sanitize(args),
            "user_id": tool_context.user_id if hasattr(tool_context, "user_id") else "unknown",
            "session_id": tool_context.session.id
            if hasattr(tool_context, "session") and tool_context.session
            else "unknown",
        }

        logger.info(
            "tool_attempt: %s.%s",
            entry["agent"],
            entry["tool"],
            extra={
                "event": entry["event"],
                "agent": entry["agent"],
                "tool": entry["tool"],
                "tool_args": entry["args"],
                "user_id": entry["user_id"],
                "session_id": entry["session_id"],
            },
        )
        _write_file_entry(resolved_path, entry)
        return None

    return callback


def audit_logger(log_path: str | Path | None = None) -> Callable:
    """Create an after_tool_callback that logs every tool invocation.

    Each log entry includes timestamp, agent, tool name, arguments,
    result status, and user/session IDs.

    The entry is always emitted via ``logging.getLogger("orrery.audit")``.
    When ``setup_logging()`` is active this produces structured JSON on
    stdout — ready for Loki, ELK, or Cloud Logging.

    Args:
        log_path: Optional path to *also* write a local .jsonl file.
                  Useful for local development. Set to ``None`` (default)
                  to rely solely on the logging system.

    Usage:
        create_agent(
            ...,
            after_tool_callback=audit_logger(),           # stdout only
            # after_tool_callback=audit_logger("audit.jsonl"),  # stdout + file
        )
    """
    resolved_path = _resolve_log_path(log_path)

    def callback(
        *,
        tool: BaseTool,
        args: dict[str, Any],
        tool_context: Context,
        tool_response: dict,
    ) -> dict | None:
        sanitized_response = _sanitize(tool_response) if isinstance(tool_response, dict) else None

        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "agent": tool_context.agent_name if hasattr(tool_context, "agent_name") else "unknown",
            "tool": tool.name,
            "args": _sanitize(args),
            "status": sanitized_response.get("status", "unknown")
            if sanitized_response is not None
            else "ok",
            "response": sanitized_response,
            "user_id": tool_context.user_id if hasattr(tool_context, "user_id") else "unknown",
            "session_id": tool_context.session.id
            if hasattr(tool_context, "session") and tool_context.session
            else "unknown",
        }

        # Emit via the logging system (structured JSON when setup_logging() is active)
        logger.info(
            "tool_call: %s.%s",
            entry["agent"],
            entry["tool"],
            extra={
                "agent": entry["agent"],
                "tool": entry["tool"],
                "tool_args": entry["args"],
                "status": entry["status"],
                "response": entry["response"],
                "user_id": entry["user_id"],
                "session_id": entry["session_id"],
            },
        )

        # Optional file fallback for local dev
        _write_file_entry(resolved_path, entry)

        return None  # don't modify the result

    return callback


_SENSITIVE_KEYS = {"password", "secret", "token", "api_key", "credential"}


def _sanitize(data: Any) -> Any:
    """Recursively redact sensitive values from dicts and lists."""
    if isinstance(data, dict):
        return {
            k: "***" if any(s in k.lower() for s in _SENSITIVE_KEYS) else _sanitize(v)
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [_sanitize(item) for item in data]
    return data


# Keep backward-compatible alias
_sanitize_args = _sanitize
