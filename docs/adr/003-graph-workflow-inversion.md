# ADR-003: Hybrid Graph-Based Workflow Root

**Status:** Accepted
**Date:** 2026-06-07
**Author:** Taoufiq
**Supersedes:** [ADR-002](002-agent-tool-vs-sub-agents.md)

## Context

ADK 2.0 introduced the **Workflow Runtime** — a graph-based execution engine where
agents, functions, and tools are nodes connected by edges. The legacy workflow agents
(`SequentialAgent`, `ParallelAgent`, `LoopAgent`) are **deprecated** in 2.0 ("Please
use Workflow instead") and emit `DeprecationWarning` on construction.

The previous root (ADR-002) was an **LLM orchestrator** (`orrery_assistant`, an
`LlmAgent`) that:
- exposed six specialist agents as `AgentTool`s and let the LLM route by intent, and
- kept a deterministic `incident_triage_agent` (`SequentialAgent`) as a sub-agent.

The `Workflow` class is **not** a `BaseAgent`, so it cannot be nested as a sub-agent
or wrapped in `AgentTool`. The graph engine is designed to sit at the **top** of the
tree with agents as nodes. We therefore had a fork: keep the LLM root and wrap a few
pipelines in Workflows, or invert fully — make a `Workflow` the root.

**Decision driver:** we want *both* a deterministic, auditable incident-response
pipeline (triage → remediate → report) **and** the open-ended conversational routing
to specialists that the LLM root provided. Since an `LlmAgent` **is** a valid graph
node, we make a `Workflow` the root and keep the conversational orchestrator as a node
on it — a **hybrid**. (An earlier revision of this ADR removed the LLM root entirely;
that "full inversion" was reverted to preserve free-form specialist routing.)

## Validated API facts (ADK 2.2.0)

- `LlmAgent` **is** a `BaseNode`, so existing `create_agent()` results are valid nodes.
- `App(root_agent=...)` accepts a `Workflow` (`BaseAgent | Any | None`), so
  `create_persistent_runner` / `create_app` need **no change**.
- Edge forms: sequential chain `("START", a, b, c)`; parallel fan-out
  `("START", (a, b, c))`; conditional/loop via `RoutingMap` `(node, {"k": x, ...})`.
- A node sets `ctx.route = <bool|int|str>` to pick a `RoutingMap` branch, and
  `ctx.state[...]` for shared data (LLM agents still use `output_key`).
- `JoinNode` (`_requires_all_predecessors = True`) is the fan-in **barrier**: it waits
  for all parallel predecessors before firing the successor.

These were confirmed with two runnable spikes (bounded loop; parallel + JoinNode).

## Decision

Make a top-level `Workflow` (`orrery_workflow`) the root. An `intent_router`
`FunctionNode` dispatches each turn between the conversational orchestrator and the
deterministic triage pipeline:

```
START
  ▼
intent_router (FunctionNode: reads ctx.user_content)
  ├─("chat", default)─▶ orrery_chat_agent (LlmAgent + 6 specialist AgentTools + memory)
  │                       └▶ free-form LLM routing to a specialist (terminal)
  └─("triage")─▶ kafka_health_checker ─┐
                 k8s_health_checker    │
                 docker_health_checker ├─▶ health_join (JoinNode, waits for all 5)
                 observability_…       │        │
                 elasticsearch_…       ┘        ▼
                                   triage_summarizer (LLM; record_triage_verdict →
                                                      incident_severity + triage_report)
                                          │
                                          ▼
                                   journal_writer (LLM, saves report + logs)
                                          │
                                          ▼
                                   triage_route (FunctionNode: "remediate" | "resolved",
                                                 with missing-verdict inference fallback)
                     ┌────────────────────┴───────────────────┐
             "remediate"                                   "resolved"
                     ▼                                          ▼
            remediation_actor                            final_report (END)
                     │
                     ▼
            remediation_verifier (LLM; mark_remediation_resolved tool)
                     │
                     ▼
            verify_route (FunctionNode: bumps remediation_iteration;
                          "retry" if unresolved and < MAX_ITER else "done")
             ┌───────┴────────┐
         "retry"           "done"
             ▼                 ▼
      remediation_actor   remediation_summarizer ─▶ final_report (END)
```

Misclassification is safe: only explicit health-sweep phrases divert to the pipeline;
everything else (including targeted "is kafka healthy?") falls through to the chat
agent, which can answer anything by routing to a specialist.

### Mapping from the deprecated agents

| Old construct | New construct |
|---|---|
| `health_check_agent` (`ParallelAgent`) | fan-out tuple `("START", (…5 checkers…))` + `health_join` (`JoinNode`) |
| `incident_triage_agent` (`SequentialAgent`) | chain `health_join → triage_summarizer → journal_writer` |
| `remediation_loop` (`LoopAgent`, max=3) | `actor → verifier → verify_route` with `RoutingMap {"retry": actor, "done": summarizer}`, bounded by `remediation_iteration` counter in state |
| `remediation_pipeline` (`SequentialAgent`) | the remediation subgraph above |
| `exit_loop` tool (`actions.escalate`) | `mark_remediation_resolved` tool + `verify_route` reading state |
| Root `LlmAgent` + 6 `AgentTool`s | **preserved** as `orrery_chat_agent` — a graph node on the `"chat"` branch, reached via `intent_router` |

### Routing decisions are deterministic FunctionNodes

`triage_summarizer` emits a structured `incident_severity` (`healthy | degraded |
critical`) into state alongside its prose `triage_report`. `triage_route` and
`verify_route` are pure Python `FunctionNode`s — unit-testable without an LLM. The
`MAX_REMEDIATION_ITERATIONS = 3` cap is enforced by a state counter, replacing
`LoopAgent.max_iterations`.

## Consequences

### Positive
- **Conversational routing preserved** — `orrery_chat_agent` keeps free-form LLM
  delegation to the six specialist `AgentTool`s, so users can still ask arbitrary
  targeted questions of the root.
- **No deprecation warnings** — the deprecated workflow agents are gone from the root.
- **Deterministic & auditable** — the triage/remediation path is a fixed graph; routing
  logic lives in testable functions, not LLM discretion. The `triage_route` fallback
  means a missing LLM verdict never silently skips a degraded system (fail-safe, flagged).
- **Same building blocks** — health-checker / summarizer / actor / verifier / specialist
  `LlmAgent`s are reused unchanged as nodes; only the wiring changed.
- **Plugins/RBAC/metrics unchanged** — they attach at the `App`/`Runner` level. The Slack
  and Google Chat confirmation walkers were extended to traverse `graph.nodes` so guarded
  destructive tools still fire interactive approvals.

### Negative / breaking
- **`planner_routing` eval removed** — it asserted LLM→AgentTool routing against the old
  `LlmAgent` root module. The chat routing now lives behind `intent_router`; the graph is
  covered by deterministic unit + end-to-end flow tests instead. (Re-adding a chat-branch
  eval is a good follow-up.)
- **Intent routing is keyword-based** — explicit health-sweep phrases trigger the
  pipeline; everything else defaults to chat. A missed triage phrase degrades to the
  conversational path (which can still answer), never to a blocked request.
- **ADR-002 superseded** — the AgentTool-vs-sub-agent decision no longer governs the root
  shape (kept for historical context; the AgentTool guidance still applies inside
  `orrery_chat_agent`).

### Neutral
- Specialist agents still build with planners; planner wiring is unaffected.
- `runner.py` / `server.py` accept a `Workflow` root (`Agent | Workflow`); no behavioral
  change beyond the widened type.

## Implementation

- `agents/orrery-assistant/orrery_assistant/agent.py` — define node agents,
  `orrery_chat_agent` (conversational LLM + 6 AgentTools + memory), `intent_router`, and
  `orrery_workflow` (`Workflow`); export it as `root_agent`.
- `agents/orrery-assistant/orrery_assistant/remediation.py` — expose actor/verifier/
  summarizer nodes + `verify_route` + `mark_remediation_resolved`; drop `LoopAgent`/
  `SequentialAgent`.
- `agents/{slack-bot,google-chat-bot}` — confirmation walkers traverse `graph.nodes`.
- `core/orrery_core` — removed the deprecated `create_sequential/parallel/loop_agent`
  factories; `run_persistent`/`create_app` accept `Agent | Workflow`.
- Tests: `test_graph_flow.py` (end-to-end execution incl. dispatch + loop cap),
  `test_planner_wiring.py` (graph structure, intent router, routing). `planner_routing`
  eval removed.
