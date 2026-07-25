"""Run the deterministic incident-triage Workflow once (batch / scheduled).

Unlike the interactive ``orrery_chat_agent`` root, this drives the graph-native
``orrery_triage_workflow`` end-to-end: parallel health checks → triage → journal
→ conditional closed-loop remediation. Intended for cron / CI / on-call sweeps.

Usage:
    uv run python run_triage.py

This run is unattended, so **no mutating tool executes**, by two independent
gates rather than one:

- ``remediation_actor`` wires ``require_confirmation()``, so every ``@confirm``
  and ``@destructive`` tool returns ``confirmation_required``. Nothing here can
  answer that prompt — a model re-call inside the same invocation does not count
  as a confirmation — so the loop reports the action it would take.
- RBAC pins the session to ``operator``, so ``@destructive`` tools (restart,
  rollback) are additionally refused outright as ``access_denied``.

Both matter. The confirmation gate is what stops ``@confirm`` tools such as
``scale_deployment``, which the ``operator`` role permits and which therefore did
run unattended before the actor was gated. Do not raise the role here without
keeping the actor's gate.
"""

import asyncio

from google.adk.apps import App
from google.adk.runners import InMemoryRunner
from google.genai import types

from orrery_assistant.agent import orrery_triage_workflow
from orrery_core import (
    create_events_compaction_config,
    default_plugins,
    load_agent_env,
    set_user_role,
)

load_agent_env(__file__)

APP_NAME = "orrery_triage"


async def main() -> None:
    app = App(
        name=APP_NAME,
        root_agent=orrery_triage_workflow,
        plugins=default_plugins(enable_memory=True),
        # A full sweep fans out to five specialists and can loop through
        # remediation three times, so this root produces the longest transcripts
        # of any surface. The explicit summarizer in the factory is required
        # here: the root is a Workflow, and ADK raises rather than deriving a
        # summarizer model from a non-LlmAgent root.
        events_compaction_config=create_events_compaction_config(),
    )
    runner = InMemoryRunner(app=app)

    # Operator role so every read-only check runs. Mutations are stopped by the
    # actor's confirmation gate (and destructive ones by RBAC on top) — see the
    # module docstring; both gates are load-bearing on this path.
    state: dict[str, object] = {}
    set_user_role(state, "operator")
    session = await runner.session_service.create_session(
        app_name=APP_NAME, user_id="batch", state=state
    )

    msg = types.Content(role="user", parts=[types.Part(text="run a full triage")])
    async for event in runner.run_async(user_id="batch", session_id=session.id, new_message=msg):
        output = getattr(event, "output", None)
        if output is not None:
            print(output)


if __name__ == "__main__":
    asyncio.run(main())
