# AEP-002: Agent Evaluation Framework

| Field | Value |
|-------|-------|
| **Status** | <span class="badge badge--green">completed</span> |
| **Priority** | <span class="badge badge--red">P0</span> |
| **Effort** | High (5-7 days) |
| **Impact** | Critical |
| **Dependencies** | None |

## Gap Analysis

### Current Implementation
The project has **439+ unit tests** with mocked external dependencies. These tests verify:
- Individual tool functions return correct data
- Input validation rejects bad inputs
- RBAC blocks unauthorized users
- Plugins execute in correct order

Additionally, **27 agent-level evaluation scenarios** across 4 agents now verify that the LLM chooses the correct tools for user queries.

### What ADK Provides
ADK has a comprehensive **evaluation framework** with:

1. **Test files** (`.test.json`): Unit-test-like evaluation of single sessions
   - Expected tool trajectory (ordered list of tool calls with args)
   - Expected final response (reference text)
   - Expected intermediate agent responses (for multi-agent)

2. **Eval sets** (`.evalset.json`): Integration-test-like evaluation of complex multi-turn sessions

3. **Built-in evaluation criteria**:
   - `tool_trajectory_avg_score`: Exact match of tool call sequence (default: 1.0)
   - `response_match_score`: ROUGE-1 similarity (default: 0.8)
   - `final_response_match_v2`: LLM-judged semantic match
   - `rubric_based_final_response_quality_v1`: Custom rubric evaluation
   - `rubric_based_tool_use_quality_v1`: Tool usage quality
   - `hallucinations_v1`: Groundedness check
   - `safety_v1`: Safety/harmlessness

4. **User simulation**: Dynamic user prompts for conversation scenario testing

5. **Three execution modes**: Web UI (`adk web`), pytest, CLI (`adk eval`)

## Current Implementation

### Eval Coverage by Agent

| Agent | Test File | Scenarios | Tools Covered |
|-------|-----------|-----------|---------------|
| **kafka-health** | `test_kafka_eval.py` | 6 | `get_kafka_cluster_health`, `list_kafka_topics`, `list_consumer_groups`, `get_topic_metadata`, `describe_consumer_groups`, `get_consumer_lag` |
| **k8s-health** | `test_k8s_eval.py` | 9 | `get_cluster_info`, `get_nodes`, `list_namespaces`, `list_pods`, `describe_pod`, `get_pod_logs`, `list_deployments`, `get_deployment_status`, `get_events` |
| **docker** | `test_docker_eval.py` | 6 | `list_containers`, `inspect_container`, `get_container_logs`, `get_container_stats`, `docker_compose_status`, `list_images` |
| **observability** | `test_observability_eval.py` | 6 | `query_prometheus`, `get_prometheus_targets`, `query_loki_logs`, `get_loki_labels`, `get_active_alerts`, `get_silences` |

### Directory Structure

```
agents/
  kafka-health/tests/
    evals/
      test_config.json              # criteria: tool_trajectory_avg_score=1.0
      cluster_health.test.json      # 3 scenarios: health, topics, consumer groups
      topic_operations.test.json    # 3 scenarios: metadata, describe groups, lag
    test_kafka_eval.py              # mocks AdminClient, runs AgentEvaluator

  k8s-health/tests/
    evals/
      test_config.json
      cluster_overview.test.json    # 3 scenarios: cluster info+nodes, nodes, namespaces
      workloads.test.json           # 6 scenarios: pods, describe, logs, deployments, status, events
    test_k8s_eval.py                # mocks CoreV1Api, AppsV1Api, VersionApi

  docker-agent/tests/
    evals/
      test_config.json
      container_ops.test.json       # 6 scenarios: list, inspect, logs, stats, compose, images
    test_docker_eval.py             # mocks _run_docker with JSON responses

  observability/tests/
    evals/
      test_config.json
      prometheus_queries.test.json  # 2 scenarios: query, targets
      loki_and_alerts.test.json     # 4 scenarios: loki logs, labels, alerts, silences
    test_observability_eval.py      # mocks _http_get with path routing
```

### Evaluation Criteria

Currently only **tool trajectory** is evaluated:

```json
{
  "criteria": {
    "tool_trajectory_avg_score": 1.0
  }
}
```

**Why trajectory-only**: `response_match_score` (ROUGE-1) is unsuitable for agent evals because LLM responses are non-deterministic — the same correct answer phrased differently scores 0.0-0.3 on ROUGE. Tool trajectory is deterministic and verifies the most important property: **did the agent call the right tool with the right arguments?**

### Running Evals

```bash
make eval  # runs all 27 eval scenarios (requires LLM credentials)
make test  # runs 439 unit tests, skips evals
```

Evals are gated behind `@pytest.mark.eval` and skip when no LLM credentials are available. CI runs evals via the `eval` job (manual trigger or `run-eval` label).

### Lessons Learned

Writing effective agent evals requires attention to LLM behavior:

1. **Prompts must be specific** — "Describe the web-app-1 pod" causes the LLM to ask "which namespace?" instead of calling the tool. Use "Describe the pod web-app-1 in the default namespace."

2. **Expected trajectories must match agent instructions** — if the agent's instruction says "Start with get_cluster_info and get_nodes for an overview", expect both tools in the trajectory, not just one.

3. **Include all args the LLM will pass** — if the user says "in the default namespace", the LLM passes `namespace: "default"` explicitly, even though it's the tool's default value.

4. **Avoid ambiguous tool names** — "Are there firing alerts in Prometheus?" causes the LLM to use `get_active_alerts` (Alertmanager) instead of `get_prometheus_alerts` (Prometheus). Make prompts unambiguous.

5. **Mock data format must match the tool** — Docker tools use `--format json`; mocks must return JSON, not table output.

6. **Test file names must be unique** — pytest collects across agents in one process. `test_agent_eval.py` in multiple agents causes import collisions. Use `test_kafka_eval.py`, `test_k8s_eval.py`, etc.

7. **`.env` files are needed per agent** — when running all evals in one pytest process, `load_agent_env()` leaks env vars across agents. Each agent needs a `.env` with its model config to avoid defaulting to an unavailable model.

## Remaining Work

### Phase 2: Orchestrator Routing Evaluation

Critical for the orrery-assistant: verify the root agent delegates to the correct specialist:

```json
{
  "eval_id": "route_kafka_query",
  "conversation": [
    {
      "user_content": {"parts": [{"text": "What's the consumer lag for group payment-processors?"}]},
      "intermediate_data": {
        "tool_uses": [
          {"name": "kafka_health_agent", "args": {"request": "..."}}
        ]
      }
    }
  ]
}
```

### Phase 3: Hallucination and Safety Checks

For DevOps agents, hallucinations are dangerous (e.g., reporting a healthy cluster when it's down):

```json
{
  "criteria": {
    "hallucinations_v1": 0.95,
    "safety_v1": 0.95
  }
}
```

These require the Vertex Gen AI Evaluation Service (paid) and are not yet integrated.

## Acceptance Criteria

- [x] Each specialist agent has evaluation scenarios covering core read-only tools
- [x] Each agent has at least 5 evaluation scenarios (Kafka: 6, K8s: 9, Docker: 6, Observability: 6)
- [ ] DevOps assistant has routing evaluation (correct agent delegation) — deferred to Phase 2
- [ ] Incident triage workflow has end-to-end evaluation — deferred to Phase 2
- [x] Evaluation runs in CI/CD with `make eval`
- [x] Tool trajectory score >= 1.0 (exact match) for all scenarios
- [ ] Hallucination score >= 0.95 for health-check scenarios — requires paid Vertex AI, deferred to Phase 3
- [x] Evaluation results are visible in CI/CD output

## Notes

- ADK evaluation with Vertex Gen AI Evaluation Service is a paid service. For CI/CD, prefer the `pytest` approach with `tool_trajectory_avg_score` (free, fast, deterministic).
- `response_match_score` (ROUGE-1) is too noisy for CI — LLM response phrasing varies across runs. Use it for local debugging, not gating.
- Consider using `rubric_based_tool_use_quality_v1` for the orchestrator to validate "was the right sub-agent chosen?" without relying on exact tool name matching.
- User simulation (`adk eval` with conversation scenarios) is valuable for multi-turn debugging but too slow for CI/CD.
