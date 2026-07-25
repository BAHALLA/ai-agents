# AEP-022: Trajectory Capture & Eval Harvesting

| Field | Value |
|-------|-------|
| **Status** | <span class="badge badge--amber">proposed</span> |
| **Priority** | <span class="badge badge--blue">P2</span> |
| **Effort** | Medium (3-4 days) |
| **Impact** | Medium-High |
| **Dependencies** | AEP-002 (eval framework) — strong; AEP-013 (PII redaction) — strong |

> Pattern borrowed from the Hermes agent architecture (ShareGPT trajectory
> generation for training data). Retargeted at Orrery's biggest maintenance pain:
> hand-authoring eval scenarios.

## Gap Analysis

### Current Implementation

Orrery records tool activity in two places, but neither produces a **structured,
replayable trajectory**:

- `ActivityPlugin` (`core/orrery_core/observability/activity.py`) appends a
  one-line summary per tool call to `session.state["session_log"]` — human-
  readable, lossy (`[agent] argsummary → status`), not a machine trajectory.
- `AuditPlugin` emits per-tool audit records to the log stream — good for
  forensics, not shaped for replay or dataset use.

Eval scenarios (`agents/*/tests/evals/*.test.json`) are therefore **written by
hand**: a human invents the prompt and types the expected `tool_uses` and args.
As the AEP-002 work and the recent eval-hardening pass showed, this is slow,
error-prone, and drifts as models change.

### Why this matters now

We *just* spent real effort refreshing expected trajectories by running agents by
hand and reading off their tool calls. That manual loop is exactly what a
trajectory exporter automates: capture a real (mocked-backend) run once, and emit
a ready-to-commit `*.test.json` scenario. Two compounding payoffs:

1. **Eval authoring becomes record-and-edit** instead of write-from-scratch.
2. A corpus of real trajectories becomes available for future fine-tuning /
   distillation to a cheaper model.

### What's available

- ADK invocation events already carry the full `(user_content, tool_uses, args,
  final_response)` shape the eval format needs — the same data ADK's
  `AgentEvaluator` consumes.
- `PIIRedactionPlugin` (AEP-013) already scrubs credentials from tool results, so
  exported trajectories are safe to persist by construction.
- The eval dataset schema is a known, small JSON shape (see AEP-002).

## Proposed Solution

An opt-in exporter plugin that serializes each invocation to a structured
trajectory, plus a CLI to convert captured trajectories into eval scenarios.

### Step 1: A trajectory record

Add `core/orrery_core/observability/trajectory.py`:

```python
@dataclass
class ToolStep:
    name: str
    args: dict[str, Any]
    status: str


@dataclass
class Trajectory:
    invocation_id: str
    agent: str
    prompt: str
    steps: list[ToolStep]
    final_response: str
    at: float

    def to_eval_case(self, eval_id: str) -> dict:
        """Emit an ADK AgentEvaluator `*.test.json` eval_case."""
        return {
            "eval_id": eval_id,
            "conversation": [
                {
                    "user_content": {"parts": [{"text": self.prompt}]},
                    "final_response": {"parts": [{"text": self.final_response}]},
                    "intermediate_data": {
                        "tool_uses": [{"name": s.name, "args": s.args} for s in self.steps],
                        "intermediate_responses": [],
                    },
                }
            ],
        }
```

### Step 2: Capture plugin (off by default)

```python
class TrajectoryCapturePlugin(BasePlugin):
    """Serializes each invocation's prompt → tool calls → response to a JSONL sink."""

    def __init__(self, sink: TrajectorySink):
        super().__init__(name="trajectory_capture")
        self._sink = sink
        self._steps: dict[str, list[ToolStep]] = defaultdict(list)

    async def after_tool_callback(self, *, tool, tool_args, tool_context, result):
        # Runs AFTER PIIRedactionPlugin (registration order), so args/result are scrubbed.
        self._steps[tool_context.invocation_id].append(
            ToolStep(name=tool.name, args=dict(tool_args), status=_status_of(result))
        )
        return None

    async def after_run_callback(self, *, invocation_context):
        traj = Trajectory(
            invocation_id=invocation_context.invocation_id,
            agent=invocation_context.agent.name,
            prompt=_first_user_text(invocation_context),
            steps=self._steps.pop(invocation_context.invocation_id, []),
            final_response=_final_text(invocation_context),
            at=time.time(),
        )
        await self._sink.write(traj)
```

Sinks: `JsonlFileSink` (local dev, one line per run) and an optional
`PostgresTrajectorySink` (reuses `DATABASE_URL`) for fleet-wide capture.

### Step 3: Harvest CLI — trajectory → eval scenario

```bash
# Convert captured runs into a reviewable eval dataset
uv run python -m orrery_core.observability.harvest \
    --input trajectories.jsonl \
    --agent kafka_health_agent \
    --out agents/kafka-health/tests/evals/harvested.test.json
```

The CLI groups by agent, de-dupes near-identical prompts, and emits a
`*.test.json` a human then **reviews and trims** before committing — capture
proposes, a human disposes (never auto-commit an eval).

### Step 4: Opt-in wiring

```python
def default_plugins(..., capture_trajectories: bool | None = None):
    if capture_trajectories or os.getenv("ORRERY_TRAJECTORY_CAPTURE") == "true":
        plugins.append(TrajectoryCapturePlugin(_sink_from_env()))
```

Off by default; enabled in dev/eval-harvest runs, or in a sampled fashion in
production (`ORRERY_TRAJECTORY_SAMPLE=0.01`).

## Affected Files

| File | Change |
|------|--------|
| `core/orrery_core/observability/trajectory.py` | New — `Trajectory`, `ToolStep`, `to_eval_case()`, sinks |
| `core/orrery_core/observability/harvest.py` | New — CLI: trajectories → `*.test.json` |
| `core/orrery_core/plugins/trajectory_plugin.py` | New — `TrajectoryCapturePlugin` |
| `core/orrery_core/plugins/__init__.py` | Wire behind `ORRERY_TRAJECTORY_CAPTURE` (after PII redaction) |
| `core/tests/test_trajectory.py` | New — capture shape, `to_eval_case` round-trips through `AgentEvaluator` |
| `docs/evals.md` | Add a "Harvesting scenarios from real runs" section |

## Acceptance Criteria

- [ ] `TrajectoryCapturePlugin` serializes prompt → ordered tool calls+args → final response
- [ ] Plugin registers **after** `PIIRedactionPlugin` so captured args/results are scrubbed
- [ ] `Trajectory.to_eval_case()` output loads and runs under ADK's `AgentEvaluator`
- [ ] Harvest CLI groups by agent, de-dupes, writes a reviewable `*.test.json`
- [ ] JSONL sink for local dev; optional Postgres sink behind `DATABASE_URL`
- [ ] Off by default; enabled via env, with optional sampling in production
- [ ] Docs explain the capture → review → commit loop (never auto-commit)

## Notes

- **Never auto-commit harvested evals.** Capture proposes candidate scenarios; a
  human must review the trajectory (is this behavior *correct*, or a bug we'd be
  freezing in?) before it becomes a regression test. This is the same lesson from
  the eval-hardening pass: a trajectory is only a good expectation if the behavior
  is genuinely right.
- Redaction is load-bearing here — trajectories persist tool output, so capture
  **must** sit downstream of `PIIRedactionPlugin`; a unit test should assert a
  seeded credential never appears in a captured trajectory.
- The fine-tuning corpus is a bonus, not the driver — the immediate win is
  killing the hand-authoring of eval scenarios.
