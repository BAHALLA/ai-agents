# Runbook: High Tool Error Rate

**Alert:** `OrreryHighToolErrorRate` · **Severity:** warning · **Owner:** @ai-platform-team
**Auto-remediation:** circuit breaker opens after 5 consecutive failures on one tool

## Symptom

`orrery_tool_calls_total{status="error"}` is more than 10% of calls over five
minutes. The agent still answers, but its answers carry gaps — "I could not
check Kafka" — and diagnoses get less reliable.

This alert usually fires **before**
[circuit-breaker-open](circuit-breaker-open.md); catching it here is cheaper.

## Is this Orrery, or is Orrery telling you the truth?

The `error_type` label answers this directly, and it is the first thing to
look at:

| `error_type` | Reading |
|---|---|
| `ConnectionError`, `TimeoutError` | Monitored system is unhealthy — **Orrery is working** |
| `AuthenticationError`, `PermissionError` | Orrery's credentials or RBAC drifted |
| `ValidationError` | The **model** is calling tools with bad arguments |
| `KnowledgeBackendError` | See [knowledge-index-unavailable](knowledge-index-unavailable.md) |

`ValidationError` is the interesting one: it means no backend was ever
contacted. Nothing is down; the agent is failing to use its own tools, which is
a prompt, schema or model-quality problem.

## Diagnosis

```bash
export NS=orrery
kubectl -n $NS port-forward svc/orrery-assistant 9100:9100 &

# Which tool, and what kind of error?
curl -s localhost:9100/metrics | grep '^orrery_tool_errors_total' | sort -t' ' -k2 -rn | head -20

# Error share per tool
curl -s localhost:9100/metrics | grep '^orrery_tool_calls_total' | grep 'status="error"'
```

**Concentrated on one tool** → that tool's backend, or that tool's schema.

**Spread across tools of one agent** → that agent's backend or credentials.

**Spread across every agent** → Orrery-side: egress, DNS, or the node.

Read a few actual failures rather than only the counters:

```bash
kubectl -n $NS logs -l app.kubernetes.io/name=orrery-assistant --tail=500 \
  | jq -r 'select(.levelname=="ERROR") | "\(.asctime) \(.name): \(.message)"' | tail -30
```

Client exception strings are scrubbed of internal hosts and paths before they
reach logs (CWE-209), so a message may be less specific than the underlying
exception. That is intentional, not a logging bug.

## Immediate mitigation

**Backend errors:** nothing to do in Orrery. The breaker will open if it gets
worse, which is the correct outcome. Hand off to the backend owner.

**Auth or permission errors:** check the service account first — this is
usually an RBAC change, not an expired token.

```bash
kubectl -n $NS auth can-i --list --as=system:serviceaccount:$NS:orrery-assistant | head -20
```

**`ValidationError` spike after a model change:** roll the model back. A model
that mis-calls tools produces confident, wrong diagnoses, which is worse than
being unavailable.

```bash
kubectl -n $NS rollout undo deploy/orrery-assistant
```

## Root cause investigation

- Correlate the error onset with deploys, credential rotations and backend
  maintenance windows.
- For `ValidationError`, read the arguments the model actually sent — the tool
  description is usually the fix, not the validator.
- Check whether one caller's usage pattern drives it. A user repeatedly asking
  for something a tool cannot express will generate errors indefinitely.

## Permanent fix

- Tighten the tool's input schema or description so the bad call is impossible
  to phrase.
- Add the backend's own health signal to the readiness probe, so Orrery reports
  *not ready* instead of serving degraded answers.
- If a tool is chronically flaky and non-essential, consider excluding it from
  the triage sweep so it stops polluting verdicts.

## Related

- [circuit-breaker-open](circuit-breaker-open.md) — what happens if this continues
- [Metrics reference](../metrics.md)
- [Tool results](../tool-results.md)
