# Runbook: Unauthorized Approval Attempt

**Alerts:** `OrreryUnauthorizedApprovalAttempt` (critical), `OrreryUnattributableApprovals` (warning)
**Owner:** @ai-platform-team · **Auto-remediation:** the gate already refused it

## Symptom

Someone other than the requester tried to approve a guarded action, or an
approval could not be attributed to any verified caller.

**The action did not run.** `require_confirmation()` is fail-closed: an
approval is only accepted from the same verified actor who triggered the
pending, and only when the decision post-dates it. This alert reports that the
control engaged — which the platform could not do at all before AEP-024.

## Is this Orrery, or is Orrery telling you the truth?

This alert is about a **person**, not a system, which makes it the odd one out
in this directory. Three readings:

| Reading | Signal |
|---|---|
| **Confused operator** | One refusal, a colleague replying "approve" in a shared thread for someone else's action |
| **Escalation attempt** | Repeated refusals, or refusals against `@destructive` tools specifically |
| **Broken transport** | `unknown_requester` rather than `not_requester`, at a steady rate — nobody is attacking, the actor stamp is missing |

The third is a different incident entirely and is the more likely of the two
alerts to fire. Check the `reason` label first.

## Diagnosis

```bash
export NS=orrery
kubectl -n $NS port-forward svc/orrery-assistant 9100:9100 &

# Which reason, and against which tool?
curl -s localhost:9100/metrics | grep '^orrery_confirmations_refused_total'
```

Then read the events themselves. `confirmation_refused` names both parties:

```bash
kubectl -n $NS logs -l app.kubernetes.io/name=orrery-assistant --tail=2000 \
  | jq -r 'select(.event=="confirmation_refused")
           | "\(.timestamp) \(.reason) tool=\(.tool) requester=\(.requester) attempted_by=\(.attempted_by)"'
```

**Correlate with the action it targeted.** `confirmation_id` ties the refusal
to the raise, and the raise carries the sanitized arguments — so you can see
exactly what someone tried to approve:

```bash
CID=<confirmation_id from above>
kubectl -n $NS logs -l app.kubernetes.io/name=orrery-assistant --tail=5000 \
  | jq -r "select(.confirmation_id==\"$CID\")"
```

**Confirm nothing executed.** This is the question that matters:

```bash
kubectl -n $NS logs -l app.kubernetes.io/name=orrery-assistant --tail=5000 \
  | jq -r 'select(.event=="confirmation_decided") | "\(.timestamp) \(.decision) by=\(.decided_by) \(.tool)"'
```

A `confirmation_decided` entry for the same `confirmation_id` with a
`decided_by` that is *not* the requester would mean the gate let something
through. That has never been observed and should be impossible — if you see
it, stop and [escalate](escalation.md) as a security incident.

## Immediate mitigation

**`not_requester`, one occurrence:** no action. Tell the person that only the
requester can approve their own action; anyone may deny. The action is still
pending and will expire on its own.

**`not_requester`, repeated or targeting destructive tools:**
[escalate](escalation.md) as a security incident. Preserve logs before
restarting anything — a pod restart destroys the container's audit stream.

**`unknown_requester` at a steady rate:** a transport is not stamping the
turn's actor, so *nobody* can approve anything and guarded remediation is
effectively disabled. Check which surface the calls come from:

```bash
kubectl -n $NS logs -l app.kubernetes.io/name=orrery-assistant --tail=1000 \
  | jq -r 'select(.event=="confirmation_refused" and .reason=="unknown_requester") | .tool' \
  | sort | uniq -c
```

`AgentGateway` stamps `msg.user_id` automatically and `_auth.subject` is the
fallback, so an empty actor usually means a transport built its own runner
without going through the gateway.

## Root cause investigation

- **Shared threads are the usual innocent explanation.** In Slack or Google
  Chat several people see the confirmation card; only one may approve. If this
  recurs with different people each time, the prompt wording is the fix, not
  the gate.
- **Check the mode.** `confirmation_raised` records
  `mode=requester_verified` or `mode=scoped`. The scoped transports match on a
  thread key rather than the requester, so the two have different exposure.
- **Check expiry alongside.** A high `orrery_confirmations_expired_total` with
  refusals suggests operators are trying to help each other because the
  requester is away — a process problem, not a security one.

## Permanent fix

- If shared-thread confusion: make the card name the requester explicitly.
- If a transport is not stamping the actor: route it through `AgentGateway`
  rather than constructing a `Runner` directly.
- If a real escalation attempt: the audit trail now has both parties and the
  arguments. Treat it as any other unauthorized-access incident.

## Related

- [Guardrails & RBAC](../guardrails.md) — what the gate enforces
- [Escalation](escalation.md)
- [Metrics reference](../metrics.md)
