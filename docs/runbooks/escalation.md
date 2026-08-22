# Escalation

**Severity:** n/a · **Owner:** @ai-platform-team

Escalating is not a failure. Experimenting on production because you did not
want to wake someone is.

## Escalate immediately — do not diagnose first

These four classes go straight to the platform owner, regardless of how
confident you feel:

| Class | Why it cannot wait |
|-------|--------------------|
| **Unapproved destructive action** | A `@confirm`/`@destructive` tool ran without a human approval tied to the requester. That is the platform's central safety promise; treat it as a security incident and preserve the audit log before touching anything. |
| **Credential or PII exposure** | A secret reached a log, a memory entry, or a model context. `PIIRedactionPlugin` and `SecureMemoryService` both scrub on write, so a leak means a pattern gap — the blast radius is every stored session, not just this one. |
| **Data loss** | Session or memory store corruption. Stop writes before investigating. |
| **Runaway spend** | Roughly $100 of LLM spend in 15 minutes. See [high-llm-spend](high-llm-spend.md) for the emergency stop, then escalate. |

## Otherwise: escalate after 20 minutes without progress

If you have run the runbook's diagnosis and still cannot say *which system is
broken* — Orrery, or something Orrery is reporting on — that is the signal.
Twenty minutes is not a target to fill; escalate sooner if you are stuck.

## Who

1. `#ai-platform` — the team channel. Default for anything non-urgent.
2. The platform owner on the rota — for the four immediate classes above, and
   anything still unresolved after 20 minutes.
3. The owning team for the *monitored* system (Kafka, Kubernetes,
   Elasticsearch) — when diagnosis shows Orrery is reporting a genuine
   incident. Hand off with the agent's own findings; they are usually specific
   enough to skip a round of triage.

> Replace these with real names and rota links for your deployment. A runbook
> that says "escalate to the on-call" without saying who is a runbook that
> stops working at 03:00.

## What to include when you escalate

Short and specific beats complete:

- The alert name and when it fired.
- Which reading you reached: Orrery broken, or Orrery reporting correctly — and
  what ruled the other one out.
- Every command you ran that **changed** state, with its actual output.
- Anything that surprised you.

## Preserving evidence

For the security and data-loss classes, capture before you mitigate. A pod
restart destroys the container's logs, and a rollback destroys the state that
explains the incident.

```bash
export NS=orrery
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

# Logs from every replica, including any that already restarted
kubectl -n $NS logs -l app.kubernetes.io/name=orrery-assistant \
  --all-containers --prefix --tail=-1 > "orrery-logs-$STAMP.jsonl"
kubectl -n $NS logs -l app.kubernetes.io/name=orrery-assistant \
  --previous --all-containers --prefix --tail=-1 >> "orrery-logs-$STAMP.jsonl" 2>/dev/null

# Pod state and recent events
kubectl -n $NS describe pods -l app.kubernetes.io/name=orrery-assistant > "orrery-pods-$STAMP.txt"
kubectl -n $NS get events --sort-by=.lastTimestamp > "orrery-events-$STAMP.txt"
```

The audit trail lives in the structured log stream (`AuditPlugin` writes
`tool_attempt` entries), so those files are the record of what the agent tried
to do and whether a gate stopped it.

## Related

- [On-call checklist](oncall-checklist.md)
- [Runbook index](README.md)
- [Guardrails & RBAC](../guardrails.md) — what the approval gate actually enforces
