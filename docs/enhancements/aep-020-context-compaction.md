# AEP-020: Conversation Context Compaction

| Field | Value |
|-------|-------|
| **Status** | <span class="badge badge--green">completed</span> |
| **Priority** | <span class="badge badge--amber">P1</span> |
| **Effort** | Low (delivered via ADK-native compaction) |
| **Impact** | High |
| **Dependencies** | AEP-007 (context caching) — soft; AEP-003 (memory) — soft |

> Originally scoped from the Hermes agent architecture (`context_compressor.py` +
> a pluggable context-engine ABC + session lineage). On implementation the
> equivalent capability turned out to ship natively in ADK ≥ 1.16, so the
> hand-rolled design below was dropped in favour of configuring
> `EventsCompactionConfig`. See *Why the design changed*.

## Gap Analysis

Orrery bounded the size of a **single tool result** but never the
**conversation as a whole**:

- `ToolOutputCapPlugin` (`core/orrery_core/plugins/output_cap_plugin.py`) trims
  one oversized tool result to `max_tool_result_bytes` (default 4 MiB).
- `ContextCacheConfig` (`core/orrery_core/serving/runner.py`) caches a stable
  prefix but does nothing about a transcript that keeps growing.

Nothing shrank accumulated history. Because the per-result cap is 4 MiB against
Gemini's ~10 MiB request ceiling, **three capped results in history are enough to
make the next turn fail** with `400 INVALID_ARGUMENT` — the cap deferred the
failure rather than preventing it.

### Why this matters

This is a **fail-at-the-worst-time** gap. The window fills precisely during a
deep, long-running incident. The closed-loop remediation subgraph (AEP-004) and
the multi-specialist triage sweep both generate large intermediate transcripts,
so the platform's flagship flows are the ones most likely to hit it. Persistent
Postgres sessions make it worse: an incident session is long-lived by design.

## Why the design changed

The original proposal specified five new modules: a `ContextEngine` ABC, a
`SummarizingContextEngine`, a `ContextCompactionPlugin` driving it from
`before_model`, a token estimator, and a custom lineage record in session state.
ADK 2.5.0 provides all of it, and handles one case the hand-rolled design got
wrong:

- **Correct split points.** `_safe_token_compaction_split_index` and
  `_longest_self_contained_prefix` (`google/adk/apps/compaction.py`) refuse to
  separate a `function_call` from its `function_response`. The proposed
  `contents[:-keep_recent]` slice would have split tool pairs and produced
  provider 400s on exactly the tool-heavy sessions this feature targets.
- **Lineage is free.** The digest is *appended* as an event carrying
  `EventCompaction(start_timestamp, end_timestamp, compacted_content)`.
  Originals stay in the session; `_process_compaction_events`
  (`flows/llm_flows/contents.py`) filters them out only at request-assembly
  time. Lossy for the model, lossless for the record — **by construction**, with
  no custom lineage state to maintain.
- **Real token counts.** `_latest_prompt_token_count` reads
  `usage_metadata.prompt_token_count` off prior events, falling back to a
  character estimate. No `chars/4` heuristic of our own.
- **Root-agnostic.** Compaction is driven by the Runner from
  `app.events_compaction_config`, so it covers both the chat `Agent` root and
  the batch `Workflow` root.

## Implementation

### `create_events_compaction_config()`

`core/orrery_core/serving/runner.py`, mirroring `create_context_cache_config()`.
Returns a configured `EventsCompactionConfig`, or `None` when disabled — `None`
is exactly how ADK reads "no compaction", so call sites need no branching.

Env surface (documented in [general config](../config/general.md#context-compaction)):
`ORRERY_CONTEXT_COMPACTION`, `ORRERY_COMPACTION_TOKEN_THRESHOLD`,
`ORRERY_COMPACTION_RETENTION_EVENTS`, `ORRERY_COMPACTION_INTERVAL`,
`ORRERY_COMPACTION_OVERLAP`, `ORRERY_COMPACTION_MODEL`.

**On by default**, at a 250k-token threshold chosen to sit well under a 1M-token
window while staying out of reach of ordinary sessions: turning compaction on
changes nothing except for the long investigations it exists to rescue. (The
full test suite passing unchanged with it enabled is the standing check on that.)

### Two ADK constraints that shaped it

1. **`compaction_interval` and `overlap_size` are required**, and
   `_has_sliding_window_config` is therefore always true once a config exists —
   sliding-window compaction **cannot be disabled**. It runs as a backstop on
   invocations where the token threshold did not fire, so the interval default
   is set high enough that the token trigger normally does the work.
2. **A non-`LlmAgent` root raises.** `_ensure_compaction_summarizer` raises
   `ValueError('No LlmAgent model available…')` when no summarizer is supplied
   and the root isn't an `LlmAgent` — which is what `orrery_triage_workflow` is.
   Supplying our own summarizer is load-bearing, not merely a cost optimization.

### `resolve_summarizer_model()`

`core/orrery_core/agent/base.py`, beside `resolve_model()`. Returns a `BaseLlm`
*instance* (unlike `resolve_model()`, which returns a bare string for Gemini)
because `LlmEventSummarizer(llm=...)` takes an object. Defaults to
`gemini-flash-latest`; on non-Gemini providers falls back to the agent's
`MODEL_NAME`, since no cross-provider cheap default can be assumed to exist.

### Metrics

`_ObservedEventSummarizer` (a thin `LlmEventSummarizer` subclass) increments
`orrery_context_compaction_total` and logs each compaction.

The summarizer is the observation point because **`on_event_callback` cannot see
compaction events**: the Runner appends them straight to the session service
after the agent's event generator is exhausted, so they never pass through the
plugin pipeline. The counter matters because compaction invalidates the cached
prefix on the turn it fires — a threshold tuned too low quietly erodes the
AEP-007 cache-hit rate.

## Affected Files

| File | Change |
|------|--------|
| `core/orrery_core/serving/runner.py` | `create_events_compaction_config()`, `_ObservedEventSummarizer`, wiring in `run_persistent` |
| `core/orrery_core/agent/base.py` | `resolve_summarizer_model()` + shared `_prefix_provider()` |
| `core/orrery_core/serving/gateway.py` | `events_compaction_config` param forwarded to `App` |
| `core/orrery_core/serving/server.py` | Same param on `create_app`, defaulting to the factory |
| `core/orrery_core/observability/metrics.py` | `orrery_context_compaction_total` + `track_compaction_event()` |
| `agents/*/…` | Config passed at each remaining `App`/`AgentGateway` site: assistant `agent.py`, `run_triage.py`, google-chat-bot, both Slack entrypoints |
| `core/tests/test_context_compaction.py` | New — 33 tests |

## Acceptance Criteria

- [x] Compaction fires past `ORRERY_COMPACTION_TOKEN_THRESHOLD`; `ORRERY_CONTEXT_COMPACTION=false` or a `0` threshold disables
- [x] Recent N events kept verbatim; older ones replaced by one summary event
- [x] Pre-compaction transcript preserved (audit + replay stay lossless)
- [x] Summary uses a cheap model independent of the agent's own
- [x] `orrery_context_compaction_total` exported
- [x] Enabled on every transport, including the batch `Workflow` root
- [x] Unit tests: on/off contract, env mapping, summarizer always explicit, gateway/server forwarding, metric

## Notes

- Compaction is **lossy for the model** but **lossless for the record** — that
  split is the whole point, and ADK's append-plus-filter model enforces it.
- `EventsCompactionConfig` is marked `@experimental` in ADK; the emitted
  `UserWarning` is filtered in the root `pyproject.toml`, scoped to that class.
- A long-horizon alternative is offloading old turns to `SecureMemoryService`
  and recalling on demand (AEP-003 + `load_memory`); compaction is the
  in-request, synchronous complement and the two compose well.
- Summarization sees tool output, so it runs after `PIIRedactionPlugin`
  (AEP-013) has scrubbed the results — redaction mutates results in place during
  the after-tool chain, well before compaction reads the stored events.
