# AEP-020: Conversation Context Compaction

| Field | Value |
|-------|-------|
| **Status** | <span class="badge badge--amber">proposed</span> |
| **Priority** | <span class="badge badge--amber">P1</span> |
| **Effort** | Medium (4-6 days) |
| **Impact** | High |
| **Dependencies** | AEP-007 (context caching) — soft; AEP-003 (memory) — soft |

> Pattern borrowed from the Hermes agent architecture (`context_compressor.py` +
> pluggable context-engine ABC + session lineage). Adapted to ADK plugins and the
> Orrery session/audit model.

## Gap Analysis

### Current Implementation

Orrery bounds the size of a **single tool result** but never compacts the
**conversation as a whole**:

- `ToolOutputCapPlugin` (`core/orrery_core/plugins/output_cap_plugin.py`) trims
  one oversized tool result to `max_tool_result_bytes` (default 4 MiB) so a chatty
  `logs`/wide ES result can't push a request past the Gemini/Vertex ~10 MiB limit.
- `ContextCacheConfig` (`core/orrery_core/serving/runner.py`) caches a stable
  prefix but does nothing about a transcript that keeps growing turn over turn.

There is **no** step that shrinks accumulated history. A long incident
investigation — a multi-specialist sweep, several `logs` pulls, a remediation
retry loop — grows the transcript monotonically until the whole request exceeds
the model's context window and the turn fails with `400 INVALID_ARGUMENT`
(Gemini) or a context-length error (Claude/OpenAI).

### Why this matters now

This is a **fail-at-the-worst-time** gap. The window fills precisely during a
deep, long-running incident — exactly when losing the session hurts most. The
closed-loop remediation subgraph (AEP-004) and the multi-specialist triage sweep
both generate large intermediate transcripts, so the platform's flagship flows
are the ones most likely to hit it.

### What's available

- ADK exposes `before_model` / `before_run` plugin hooks where the outgoing
  `LlmRequest.contents` can be rewritten before it reaches the provider.
- `SecureMemoryService` (AEP-003) already persists durable summaries — the same
  summarization LLM call can feed both memory and compaction.
- Token accounting already flows through `MetricsPlugin.after_model` via
  `track_llm_tokens()`, so a running token estimate per session is cheap.

## Proposed Solution

A pluggable, single-active **context engine** that watches the running transcript
and, past a token threshold, replaces older turns with a compact summary while
keeping recent turns verbatim — **without losing the original** (lineage).

### Step 1: A context-engine ABC (one active at a time)

Add `core/orrery_core/context/engine.py`:

```python
from abc import ABC, abstractmethod
from google.genai import types

class ContextEngine(ABC):
    """Rewrites conversation history before it reaches the model.

    One engine is active per Runner, chosen by env var. The default is a no-op;
    `SummarizingContextEngine` is the lossy compactor.
    """

    @abstractmethod
    def should_compact(self, contents: list[types.Content], est_tokens: int) -> bool: ...

    @abstractmethod
    async def compact(
        self, contents: list[types.Content]
    ) -> tuple[list[types.Content], str]:
        """Return (rewritten_contents, digest_text). digest_text is stored for lineage."""
```

### Step 2: A summarizing engine

```python
class SummarizingContextEngine(ContextEngine):
    def __init__(self, *, max_tokens: int, keep_recent_turns: int = 6):
        self._max = max_tokens
        self._keep = keep_recent_turns

    def should_compact(self, contents, est_tokens):
        return est_tokens > self._max and len(contents) > self._keep

    async def compact(self, contents):
        head, tail = contents[: -self._keep], contents[-self._keep :]
        # Summarize `head` with a cheap model into an "incident so far" digest.
        digest = await self._summarize(head)
        rewritten = [
            types.Content(role="user", parts=[types.Part(
                text=f"[Earlier conversation summary]\n{digest}")]),
            *tail,
        ]
        return rewritten, digest
```

Use a **cheap** model for the summary (Flash/Haiku) regardless of the agent's own
model — this is a cost/latency optimization, not user-facing reasoning.

### Step 3: A plugin that drives the engine + records lineage

```python
class ContextCompactionPlugin(BasePlugin):
    """Compacts the transcript before the model call and preserves the original."""

    def __init__(self, engine: ContextEngine):
        super().__init__(name="context_compaction")
        self._engine = engine

    async def before_model_callback(self, *, callback_context, llm_request):
        contents = llm_request.contents
        est = _estimate_tokens(contents)
        if not self._engine.should_compact(contents, est):
            return None
        rewritten, digest = await self._engine.compact(contents)
        # Lineage: stash the pre-compaction transcript so audit/replay stays lossless.
        callback_context.state.setdefault("compaction_lineage", []).append({
            "at": time.time(), "digest": digest, "dropped_turns": len(contents) - len(rewritten),
        })
        llm_request.contents = rewritten
        logger.info("context_compacted", extra={
            "est_tokens_before": est, "turns_before": len(contents), "turns_after": len(rewritten)})
        return None
```

The **lineage** entry (and, optionally, the full pre-compaction transcript written
to the memory/session store) means compaction never destroys the audit trail —
it only shrinks what the *model* sees.

### Step 4: Wire behind an env flag in `default_plugins()`

```python
def default_plugins(..., context_max_tokens: int | None = None):
    ...
    max_tokens = context_max_tokens or int(os.getenv("ORRERY_CONTEXT_MAX_TOKENS", "0"))
    if max_tokens:
        engine = SummarizingContextEngine(max_tokens=max_tokens)
        plugins.append(ContextCompactionPlugin(engine))
```

Off by default (`0` disables), mirroring the autonomy/tracing opt-in pattern.

## Interaction with context caching (AEP-007)

Compaction **invalidates** the cached prefix on the turn it fires (the history
changes), so it should fire rarely — tune `ORRERY_CONTEXT_MAX_TOKENS` well below
the hard model limit but high enough that most sessions never compact. Emit a
`context_compaction_total` counter so the cache-hit ROI stays visible.

## Affected Files

| File | Change |
|------|--------|
| `core/orrery_core/context/engine.py` | New — `ContextEngine` ABC + `SummarizingContextEngine` |
| `core/orrery_core/context/tokens.py` | New — `_estimate_tokens()` heuristic (chars/4 or provider counter) |
| `core/orrery_core/plugins/context_compaction_plugin.py` | New — the driver plugin |
| `core/orrery_core/plugins/__init__.py` | Wire into `default_plugins()` behind `ORRERY_CONTEXT_MAX_TOKENS` |
| `core/orrery_core/metrics.py` | Add `context_compaction_total` counter |
| `core/tests/test_context_compaction.py` | New — threshold, keep-recent, lineage, no-op-by-default |
| `docs/memory.md` / `docs/config/general.md` | Document the flag + interaction with caching |

## Acceptance Criteria

- [ ] `ContextEngine` ABC with a no-op default and one `SummarizingContextEngine`
- [ ] Compaction fires only past `ORRERY_CONTEXT_MAX_TOKENS`; disabled when unset/0
- [ ] Recent N turns kept verbatim; older turns replaced by one summary turn
- [ ] Pre-compaction transcript/lineage preserved (audit + replay stay lossless)
- [ ] Summary uses a cheap model independent of the agent's own model
- [ ] `context_compaction_total` metric exported
- [ ] Unit tests: threshold boundary, keep-recent, lineage record, off-by-default

## Notes

- Compaction is **lossy for the model** but **lossless for the record** — that
  split is the whole point; never drop the original transcript silently.
- A long-horizon alternative is offloading old turns to `SecureMemoryService` and
  recalling on demand (AEP-003 + `load_memory`); compaction is the in-request,
  synchronous complement and the two compose well.
- Keep the summary prompt injection-safe: it processes tool output, so run it
  after `PIIRedactionPlugin` (AEP-013) has scrubbed the results it summarizes.
