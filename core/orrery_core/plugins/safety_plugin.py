"""Prompt-injection screening for inbound user messages (AEP-013).

``SafetyScreenPlugin`` runs in ``before_run_callback`` — the one plugin hook
whose non-None return **halts the runner** and becomes the reply (ADK 2.0's
``on_user_message_callback`` can only *replace* the message, not block it).
A screened-out message therefore never reaches the model, costs no tokens,
and cannot influence any tool call.

This is the regex baseline from ADK's safety guidance: it catches the overt
override phrasings ("ignore previous instructions", "reveal your system
prompt", ...), not adversarial paraphrases. The real security boundary
remains the deterministic layer underneath — RBAC, guardrail confirmations,
autonomy gates, and input validation hold even when a novel injection gets
past this screen. For higher assurance, front it with an LLM judge
(fast/cheap model) — this plugin is deliberately dependency-free.

Matched attempts are logged (first 200 chars) for audit visibility.
"""

from __future__ import annotations

import logging
import re

from google.adk.agents.invocation_context import InvocationContext
from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types

logger = logging.getLogger("orrery.safety")

# Overt instruction-override / prompt-exfiltration phrasings. Deliberately
# narrow: an SRE pasting incident logs must not trip false positives.
INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?i)\b(ignore|disregard|forget)\b.{0,40}\b(previous|prior|above|earlier|all)\b.{0,20}\binstructions?\b"
    ),
    re.compile(r"(?i)\b(ignore|disregard|forget)\s+(your|the)\s+instructions?\b"),
    re.compile(
        r"(?i)\b(reveal|print|show|repeat|output|dump)\b.{0,30}\b(system\s+prompt|initial\s+instructions?|hidden\s+instructions?)\b"
    ),
    re.compile(r"(?i)\byou\s+are\s+no\s+longer\b.{0,40}\b(assistant|agent|bound|restricted)\b"),
    re.compile(
        r"(?i)\b(pretend|act\s+as\s+if)\b.{0,30}\bno\s+(rules|restrictions|guardrails|limitations)\b"
    ),
    re.compile(
        r"(?i)\b(override|bypass|disable)\b.{0,30}\b(safety|guardrails?|restrictions|filters?)\b"
    ),
)

REFUSAL_TEXT = (
    "I can only help with DevOps and SRE tasks. That message looks like an "
    "attempt to override my operating instructions, so I won't process it."
)


def screen_text(text: str) -> re.Pattern[str] | None:
    """Return the first matching injection pattern, or None if clean."""
    for pattern in INJECTION_PATTERNS:
        if pattern.search(text):
            return pattern
    return None


class SafetyScreenPlugin(BasePlugin):
    """Blocks messages matching prompt-injection patterns before the model runs."""

    def __init__(self, *, extra_patterns: list[re.Pattern[str]] | None = None) -> None:
        super().__init__(name="safety_screen")
        self._patterns = INJECTION_PATTERNS + tuple(extra_patterns or ())

    async def before_run_callback(
        self, *, invocation_context: InvocationContext
    ) -> types.Content | None:
        content = invocation_context.user_content
        if content is None or not content.parts:
            return None
        text = " ".join(part.text for part in content.parts if part.text)
        if not text:
            return None
        for pattern in self._patterns:
            if pattern.search(text):
                logger.warning(
                    "blocked potential prompt injection (pattern %r): %.200s",
                    pattern.pattern,
                    text,
                )
                return types.Content(role="model", parts=[types.Part(text=REFUSAL_TEXT)])
        return None
