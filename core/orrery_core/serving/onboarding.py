"""First-run diagnostics for the web console (AEP-019, Milestone 3).

Most first-run failures are not agent failures: a broker address is wrong, a
kubeconfig is missing, an API key was never set. Today the only feedback is a
stack trace buried in a tool result, which means a new user cannot tell "the
agent is broken" from "Kafka was never wired up".

This module answers two questions directly, without going through the model:

- **Can we reach the model provider?** :func:`check_model_connectivity` does a
  one-token round-trip against the configured provider/model.
- **Which integrations are actually wired?** An :class:`IntegrationProbe` is a
  named, read-only callable — a specialist's cheapest health tool — that reports
  reachable / unreachable with the reason.

Core deliberately owns no probes of its own: it has no dependency on any agent
package, so the probe list is *supplied* by whoever builds the app (see
``orrery_assistant/app.py``). That keeps the console a transport over
``AgentGateway`` rather than something that knows about Kafka.

Probes must be read-only. They run unauthenticated by RBAC standards — the
caller is authenticated, but nothing here consults their role — so anything with
side effects has no business in this list.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("orrery.onboarding")

#: A probe returns its tool's raw result dict; the shape is the platform's usual
#: ``{"status": "success" | "error", ...}``.
ProbeCallable = Callable[[], Awaitable[dict[str, Any]]]

#: Longest a single probe may take before it is reported as unreachable. A
#: first-run check that hangs is worse than one that fails: the user is sitting
#: in front of it waiting to learn whether their config works.
PROBE_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class IntegrationProbe:
    """One integration's cheapest read-only reachability check.

    Args:
        name: Stable identifier (``"kafka"``, ``"kubernetes"``, …).
        label: Human-facing name for the console.
        hint: What to configure when it fails — the single most useful thing to
            show a user whose check just went red.
        run: The read-only async callable to invoke.
    """

    name: str
    label: str
    hint: str
    run: ProbeCallable


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one probe or connectivity check."""

    name: str
    label: str
    ok: bool
    detail: str
    hint: str = ""
    duration_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "ok": self.ok,
            "detail": self.detail,
            "hint": self.hint,
            "duration_ms": self.duration_ms,
        }


def _summarize(result: Any) -> tuple[bool, str]:
    """Reduce a tool result to (ok, one-line detail).

    Tools in this platform return ``{"status": ...}`` dicts rather than raising,
    so a failed probe is an ordinary result — not an exception.
    """
    if not isinstance(result, dict):
        return False, "Probe returned an unexpected result type."
    status = str(result.get("status", "")).lower()
    if status in ("success", "ok"):
        # Prefer a field that says something concrete about what was reached.
        for key in ("health", "message", "cluster_version", "count", "brokers_online"):
            if (value := result.get(key)) not in (None, ""):
                return True, f"{key}: {value}"
        return True, "Reachable."
    message = result.get("message") or result.get("error") or "Unreachable."
    return False, str(message)[:300]


async def run_probe(probe: IntegrationProbe) -> CheckResult:
    """Run one probe, converting a hang, crash, or error result into a verdict."""
    started = time.perf_counter()
    try:
        raw = await asyncio.wait_for(probe.run(), timeout=PROBE_TIMEOUT_SECONDS)
        ok, detail = _summarize(raw)
    except TimeoutError:
        ok, detail = False, f"No response within {PROBE_TIMEOUT_SECONDS:.0f}s."
    except Exception as exc:  # noqa: BLE001 — a probe must never break the page
        # The message can carry internal hosts, but this endpoint exists
        # precisely to tell an operator *which* host is unreachable, and the
        # caller is authenticated. The class name alone would be useless here.
        logger.warning("Integration probe '%s' failed", probe.name, exc_info=exc)
        ok, detail = False, f"{type(exc).__name__}: {exc}"[:300]

    return CheckResult(
        name=probe.name,
        label=probe.label,
        ok=ok,
        detail=detail,
        hint="" if ok else probe.hint,
        duration_ms=round((time.perf_counter() - started) * 1000),
    )


async def run_probes(probes: Sequence[IntegrationProbe]) -> list[CheckResult]:
    """Run every probe concurrently — a slow integration must not serialize the rest."""
    if not probes:
        return []
    return list(await asyncio.gather(*(run_probe(probe) for probe in probes)))


async def check_model_connectivity() -> CheckResult:
    """One-token round-trip against the configured provider and model.

    This is the check that distinguishes "my credentials are wrong" from "the
    agent gave a bad answer", which is otherwise a long afternoon.
    """
    provider = os.getenv("MODEL_PROVIDER", "gemini")
    model_name = os.getenv("MODEL_NAME", "(default)")
    label = f"{provider} / {model_name}"
    started = time.perf_counter()

    try:
        from google.adk.models.llm_request import LlmRequest
        from google.genai import types

        from ..agent.base import resolve_model

        model = resolve_model()
        # A string means a Gemini model name that ADK resolves through the
        # registry; anything else is already a BaseLlm (LiteLlm).
        if isinstance(model, str):
            from google.adk.models.registry import LLMRegistry

            model = LLMRegistry.new_llm(model)

        request = LlmRequest(
            model=getattr(model, "model", None),
            contents=[types.Content(role="user", parts=[types.Part.from_text(text="ping")])],
            config=types.GenerateContentConfig(max_output_tokens=1),
        )
        async for _ in model.generate_content_async(request, stream=False):
            break  # one response is all the proof required
        ok, detail = True, f"Reached {label}."
    except Exception as exc:  # noqa: BLE001 — the point is to report the failure
        logger.warning("Model connectivity check failed", exc_info=exc)
        ok, detail = False, f"{type(exc).__name__}: {exc}"[:300]

    return CheckResult(
        name="model",
        label=label,
        ok=ok,
        detail=detail,
        hint="" if ok else "Check MODEL_PROVIDER, MODEL_NAME, and the provider's API key.",
        duration_ms=round((time.perf_counter() - started) * 1000),
    )
