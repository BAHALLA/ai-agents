# Runbook: <Alert Name>

**Severity:** critical | warning | info · **Owner:** @team · **Auto-remediation:** none | …

<!--
Keep the section headings below verbatim. They are retrieval units: the
knowledge index cites a passage as "§ Immediate mitigation", so consistent
names make citations legible and let the agent find the right part of a long
runbook. See docs/knowledge.md.
-->

## Symptom

What the alert says, and what a user would notice. One or two sentences.

## Is this Orrery, or is Orrery telling you the truth?

Almost every alert on this platform has two readings: the monitored
infrastructure is broken (Orrery is working), or Orrery is broken. Say how to
tell them apart here, first, before any mitigation. Most incidents end at this
section.

## Diagnosis

Concrete commands with the output you should expect. Not prose.

```bash
kubectl -n $NS get pods -l app.kubernetes.io/name=orrery-assistant
```

State what a healthy result looks like, so the reader can tell.

## Immediate mitigation

The first five minutes. Prefer reversible actions, and say explicitly what each
one costs — dropped sessions, lost approvals, a cold cache.

## Root cause investigation

Deeper checks once the bleeding has stopped. This section is *not* for the
first five minutes; say so if a step is slow or disruptive.

## Permanent fix

Link the PR, ADR or AEP. If there isn't one yet, say what it would need to be —
a runbook that ends "and then it happens again next month" is unfinished.

## Related

- Alerts that commonly fire alongside this one
- Related runbooks
- Dashboards
