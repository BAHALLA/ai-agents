"""PII / secret redaction for tool outputs (AEP-013).

``PIIRedactionPlugin`` scrubs credentials from every tool result before the
model, the session store, or any after-tool observer sees them. Two layers:

1. **Key-based**: dict entries whose key names a credential (``password``,
   ``access_token``, ``api_key``, ...) have their value replaced outright —
   the most reliable signal, immune to value-format drift.
2. **Pattern-based**: string values are scanned for well-known secret shapes
   (``password=...`` pairs, PEM blocks, AWS/GitHub/Slack/OpenAI token
   prefixes, JWTs) and matches are replaced in place.

Redaction **mutates the result in place and returns None** — deliberately.
ADK's after-tool chain early-exits on the first non-None return, so returning
a redacted copy would silence every observer registered later (audit outcome,
activity, metrics, output cap). Mutating the shared dict keeps the chain
intact while ensuring all downstream observers record the redacted values —
which is the point: the audit log must not carry secrets either. For the same
reason the plugin is registered *before* ``AuditPlugin`` in
``default_plugins()``.

Infrastructure identifiers (IP addresses) are **not** redacted by default —
an SRE agent that cannot see pod IPs or broker addresses cannot diagnose
anything. Opt in with ``redact_ips=True`` (``ORRERY_REDACT_IPS`` via
``default_plugins``) for compliance-bound deployments.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from ..payload import OFFLOAD_THRESHOLD_CHARS, text_volume

logger = logging.getLogger("orrery.pii")

# Redaction moves to a worker thread above OFFLOAD_THRESHOLD_CHARS. Scanning
# costs roughly 60 ms per MiB here (several regex passes over the same text), so a
# 20 MiB `get_pod_logs` result measured ~1.3 s of blocking. The threshold lives in
# `orrery_core.payload` because the safety screen makes the same call about the
# same payload, and the two must not drift apart.

REDACTED = "[REDACTED]"

# Dict keys that carry credentials. Keys are normalized to snake_case first
# (camelCase/PascalCase boundaries become underscores), then matched against
# the end of the key — so "access_token", "accessToken", and "DbPassword" all
# hit while "token_count" does not. Matching on the normalized form (rather
# than a case-insensitive camel lookaround in the pattern) keeps the
# allowlist meaningful: "nextPageToken" normalizes to the allowlisted
# "next_page_token" instead of being redacted as a token.
# "token" is singular-only: plural endings ("total_tokens", "maxTokens") are
# LLM usage counts, not credentials.
SENSITIVE_KEY_PATTERN = re.compile(
    r"(?:^|[_\-.])"
    r"(?:(?:password|passwd|pwd|secret|api[_\-]?key|apikey|authorization|"
    r"credential|private[_\-]?key|access[_\-]?key|session[_\-]?key|"
    r"client[_\-]?secret|signing[_\-]?key|cert[_\-]?key)s?|token)$"
)

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _normalize_key(key: str) -> str:
    """``dbPassword`` → ``db_password``; ``AccessToken`` → ``access_token``."""
    return _CAMEL_BOUNDARY.sub("_", key).lower()


# Keys that end in a sensitive word but are opaque cursors, not credentials.
# Compared against the *normalized* key, so camelCase variants are covered.
KEY_ALLOWLIST = frozenset({"next_page_token", "page_token", "continue_token", "resume_token"})

# Secret shapes inside free-form string values (log lines, config dumps, ...).
#
# Each entry pairs a pattern with the case-sensitive literals it cannot match
# without. Every pattern otherwise scans the full text, and on the multi-MiB log
# payloads this platform routinely handles that is the dominant cost of the
# whole after-tool chain — while the provider-token shapes almost never fire.
# ``"AKIA" in text`` is a memchr-style scan, far cheaper than running the regex
# engine over the same bytes, so an absent literal skips its pattern outright.
# Measured 2x faster on clean log text with byte-identical output.
#
# A literal here is a **correctness claim**: it must appear in every string the
# pattern can match. Anchor it on the fixed prefix, never on a character class.
# An empty tuple means "always run" — the right choice for the case-insensitive
# key=value pattern, whose triggers would need the text lowercased first (a full
# copy, which costs more than it saves).
SECRET_VALUE_PATTERNS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    # key=value / key: value credential pairs
    (
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|token|api[_\-]?key|apikey|"
            r"credential|authorization|bearer)\b\s*[:=]\s*\S+"
        ),
        (),
    ),
    # PEM private-key blocks
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
        ("-----BEGIN",),
    ),
    # Provider token prefixes: AWS access key, GitHub, Slack, OpenAI-style
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), ("AKIA",)),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), ("ghp_", "gho_", "ghu_", "ghs_", "ghr_")),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"), ("xox",)),
    (re.compile(r"\bsk-[A-Za-z0-9\-_]{20,}\b"), ("sk-",)),
    # JWT: three dot-separated base64url segments
    (
        re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
        ("eyJ",),
    ),
)

IPV4_PATTERN = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
REDACTED_IP = "[REDACTED_IP]"

_MAX_DEPTH = 32


def _redact_text(text: str, *, redact_ips: bool) -> tuple[str, int]:
    """Return *text* with secret shapes replaced, plus the replacement count."""
    count = 0
    for pattern, triggers in SECRET_VALUE_PATTERNS:
        # Skip the regex entirely when a literal the pattern requires is absent.
        if triggers and not any(trigger in text for trigger in triggers):
            continue
        text, n = pattern.subn(REDACTED, text)
        count += n
    if redact_ips:
        text, n = IPV4_PATTERN.subn(REDACTED_IP, text)
        count += n
    return text, count


def _is_sensitive_key(key: str) -> bool:
    normalized = _normalize_key(key)
    return normalized not in KEY_ALLOWLIST and bool(SENSITIVE_KEY_PATTERN.search(normalized))


def redact_structure(obj: Any, *, redact_ips: bool = False, _depth: int = 0) -> int:
    """Redact secrets in *obj* (dict/list) **in place**; return replacements made.

    Strings nested in tuples/sets are left alone (immutable containers are
    rare in tool results and rebuilding them would change object identity).
    """
    if _depth > _MAX_DEPTH:
        return 0
    count = 0
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str) and _is_sensitive_key(key) and value not in (None, ""):
                obj[key] = REDACTED
                count += 1
            elif isinstance(value, str):
                redacted, n = _redact_text(value, redact_ips=redact_ips)
                if n:
                    obj[key] = redacted
                    count += n
            else:
                count += redact_structure(value, redact_ips=redact_ips, _depth=_depth + 1)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, str):
                redacted, n = _redact_text(item, redact_ips=redact_ips)
                if n:
                    obj[i] = redacted
                    count += n
            else:
                count += redact_structure(item, redact_ips=redact_ips, _depth=_depth + 1)
    return count


class PIIRedactionPlugin(BasePlugin):
    """Scrubs credentials from tool results before anything downstream sees them."""

    def __init__(self, *, redact_ips: bool = False) -> None:
        super().__init__(name="pii_redaction")
        self._redact_ips = redact_ips

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: Any,
    ) -> None:
        # In-place + return None: a non-None return would early-exit ADK's
        # after-tool chain and skip every observer registered after this one.
        # Mutating in a worker thread is safe for the same reason it is safe
        # here — nothing else touches the result until this callback returns.
        if text_volume(result, OFFLOAD_THRESHOLD_CHARS) >= OFFLOAD_THRESHOLD_CHARS:
            count = await asyncio.to_thread(redact_structure, result, redact_ips=self._redact_ips)
        else:
            count = redact_structure(result, redact_ips=self._redact_ips)
        if count:
            logger.warning("redacted %d secret value(s) from '%s' result", count, tool.name)
        return None
