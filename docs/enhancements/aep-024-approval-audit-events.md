# AEP-024: Approval Audit Events

| Field | Value |
|-------|-------|
| **Status** | <span class="badge badge--amber">proposed</span> |
| **Priority** | <span class="badge badge--amber">P1</span> |
| **Effort** | Low (1-2 days) |
| **Impact** | Medium-High |
| **Dependencies** | AEP-001 (confirmation), AEP-013 (auth) — both completed |

## Gap Analysis

### Current Implementation

Every guarded action already produces an audit trail, and the requester-verified
gate already knows exactly who approved what:

- `AuditPlugin` writes a `tool_attempt` entry before the gates run and an
  outcome entry after, carrying `timestamp`, `agent`, `tool`, sanitized `args`,
  `user_id` and `session_id` (`core/orrery_core/observability/audit.py`).
- `require_confirmation()` in strict mode stores a pending keyed by the verified
  `requester`, and refuses any decision from a second person, an unknown
  requester, or a decision that predates the pending
  (`core/orrery_core/security/guardrails.py`).
- The decision itself is a structured record — `{"decision": …, "by": …,
  "timestamp": …}` — written into state by the gateway each turn.

So the platform **enforces** requester-verified approval correctly. What it does
not do is **record the approval as an event of its own**.

### What's missing

There is no audit entry that says "user X approved destructive tool Y at time T
for pending Z." The audit stream contains only:

1. `tool_attempt` — the call that raised the pending (status
   `AWAITING_CONFIRMATION`).
2. `tool_attempt` again — the model's retry after approval, which succeeded.

The approval is *derivable* from that pair: in strict mode the approver must be
the requester, so entry 2's `user_id` is also the approver. But deriving it
requires knowing that invariant, correlating two entries across invocations, and
trusting that strict mode was actually armed on that deployment — the audit
record itself does not say which mode was in force.

Three things are therefore unrecoverable from the log today:

- **Refused approvals.** A second person trying to approve someone else's
  pending action is refused by the gate, and that refusal is invisible. This is
  the single most interesting security event the confirmation system can
  produce, and it leaves no trace.
- **Expiry vs. abandonment.** A pending that timed out looks identical to one
  the operator never answered.
- **Mode provenance.** Whether the approval was requester-verified or the
  weaker model-mediated flow is not in the record, so an auditor cannot tell a
  human-approved production change from a model re-call in a dev surface.

### Why this matters

The platform restarts pods, rolls back deployments, alters topic configs and
resets consumer-group offsets. For any of those, "who authorized this?" is the
first question after an incident and the first question in a compliance review.
An answer that requires reconstructing an invariant from two correlated entries
is not an audit trail — it is forensics.

This is also the cheapest remaining item in the security perimeter. The data
already exists at the decision point; it is simply not emitted.

## Proposed Solution

Emit approval lifecycle events from the confirmation gate, on the same logging
path as `AuditPlugin`, using the same JSON formatter and correlation fields.

### Step 1: A pending identity

Give each pending a stable `confirmation_id` (uuid4) at creation, stored
alongside the existing pending record in both `ConfirmationStore` backends. It
is the correlation key that ties the raise, the decision and the execution
together, and it is what an operator quotes in a postmortem.

### Step 2: Four events

| Event | Emitted when | Key fields |
|-------|--------------|------------|
| `confirmation_raised` | A `@confirm`/`@destructive` tool call is paused | `confirmation_id`, `tool`, sanitized `args`, `requester`, `mode`, `expires_at` |
| `confirmation_decided` | A decision is accepted | `confirmation_id`, `decision`, `decided_by`, `latency_ms` |
| `confirmation_refused` | A decision is rejected by the gate | `confirmation_id`, `attempted_by`, `reason` |
| `confirmation_expired` | A pending ages out unanswered | `confirmation_id`, `age_ms` |

`mode` records `requester_verified` or `model_mediated`, closing the provenance
gap. `reason` on a refusal is an enum — `not_requester`, `unknown_requester`,
`stale_decision`, `no_pending` — not free text, so it is aggregatable.

### Step 3: Reuse the existing redaction and sizing rules

Approval events carry tool arguments, so they go through the same `_sanitize()`
path `AuditPlugin` already uses, and respect `MAX_AUDIT_RESPONSE_CHARS`. No new
redaction surface: the arguments recorded here are the ones the gate already
holds.

### Step 4: Metrics and a refusal alert

`MetricsPlugin` gains three counters — `orrery_confirmations_raised_total`,
`orrery_confirmations_decided_total{decision}`,
`orrery_confirmations_refused_total{reason}` — plus a histogram of
decision latency (how long humans actually take, which is useful capacity data
for the pending TTL).

`orrery_confirmations_refused_total{reason="not_requester"}` deserves an alert
rule in `infra/alert_rules.yml`: a non-requester attempting to approve a
destructive action is either a confused operator or an attempt to escalate, and
both are worth a page.

### Step 5: Surface in the console

The web console's approval panel already lists the caller's pending actions.
Add the `confirmation_id` to that view so an operator can quote it, and show
decision history for the session in the activity timeline.

## Affected Files

| File | Change |
|------|--------|
| `core/orrery_core/security/confirmation_store.py` | Add `confirmation_id` to the pending record (both backends) |
| `core/orrery_core/security/guardrails.py` | Emit the four events at raise / decide / refuse / expire |
| `core/orrery_core/observability/audit.py` | Shared `audit_event()` helper so approval events match `tool_attempt` shape |
| `core/orrery_core/observability/metrics.py` | Three counters + decision-latency histogram |
| `infra/alert_rules.yml` | Alert on `not_requester` refusals; `runbook_url` annotation (AEP-017) |
| `web/src/` | Show `confirmation_id`; decision history in the timeline |
| `core/tests/test_guardrails.py` | Assert an event per outcome, including refusals |
| `docs/guardrails.md` | Document the event schema |

## Acceptance Criteria

- [ ] Every pending carries a stable `confirmation_id` across both store backends
- [ ] All four lifecycle events emitted on the structured logging path
- [ ] `mode` distinguishes requester-verified from model-mediated in the record
- [ ] A refused approval leaves an entry naming the attempted approver and reason
- [ ] Arguments in approval events pass through the existing `_sanitize()` path
- [ ] Prometheus counters for raised / decided / refused, plus decision latency
- [ ] Alert rule on non-requester approval attempts
- [ ] Tests cover the refusal paths — these are the entries that did not exist before
- [ ] Console shows `confirmation_id` and per-session decision history

## Notes

- **The gate does not change.** This AEP is observability only: no new
  authorization logic, no change to who may approve what. The fail-closed
  behaviour of `require_confirmation()` is already correct — it is silent, not
  wrong.
- **Refusals are the point.** Successful approvals are partly reconstructable
  today; refusals are not recorded anywhere at all. If this AEP is trimmed for
  time, `confirmation_refused` is the event to keep.
- Deliberately kept off BigQuery. The approval trail must survive with the same
  guarantees as the rest of the audit log and must not depend on a cloud
  analytics sink being reachable — see AEP-026, which is explicit that BigQuery
  is an analytics surface, not a system of record.
