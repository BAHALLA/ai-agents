"""Tests for the identity-aware InstructionProvider in orrery_core.agent.base."""

from __future__ import annotations

from types import SimpleNamespace

from orrery_core.agent.base import base_instruction, identity_aware_instruction

PROMPT = 'You are a helpful SRE agent. Literal braces are fine: {"status": "ok"}'


def _ctx(state: dict | None) -> SimpleNamespace:
    return SimpleNamespace(state=state if state is not None else {})


def test_provider_returns_base_when_no_identity():
    provider = identity_aware_instruction(PROMPT)
    assert provider(_ctx({})) == PROMPT
    assert provider(SimpleNamespace()) == PROMPT  # no state at all


def test_provider_appends_actor_identity():
    provider = identity_aware_instruction(PROMPT)
    out = provider(_ctx({"actor": "alice@example.com"}))
    assert out.startswith(PROMPT)
    assert "alice@example.com" in out
    assert "Who you are talking to" in out


def test_provider_falls_back_to_auth_subject():
    provider = identity_aware_instruction(PROMPT)
    out = provider(_ctx({"_auth": {"subject": "bob@example.com", "role": "operator"}}))
    assert "bob@example.com" in out


def test_actor_takes_precedence_over_auth_subject():
    provider = identity_aware_instruction(PROMPT)
    out = provider(_ctx({"actor": "alice@example.com", "_auth": {"subject": "bob@example.com"}}))
    assert "alice@example.com" in out
    assert "bob@example.com" not in out


def test_base_instruction_unwraps_provider_and_passes_strings():
    provider = identity_aware_instruction(PROMPT)
    assert base_instruction(provider) == PROMPT
    assert base_instruction(PROMPT) == PROMPT
    agent = SimpleNamespace(instruction=provider)
    assert base_instruction(agent) == PROMPT


def test_create_agent_wraps_instruction():
    from orrery_core.agent.base import create_agent

    agent = create_agent(
        name="test_agent",
        description="d",
        instruction=PROMPT,
        tools=[],
        model="gemini-2.0-flash",
    )
    assert callable(agent.instruction)
    assert base_instruction(agent) == PROMPT
