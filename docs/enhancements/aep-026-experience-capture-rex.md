# AEP-026: Experience Capture & REX Generation

| Field | Value |
|-------|-------|
| **Status** | <span class="badge badge--amber">proposed</span> |
| **Priority** | <span class="badge badge--blue">P2</span> |
| **Effort** | Medium-High (1-2 weeks) |
| **Impact** | Medium-High |
| **Dependencies** | AEP-025 (corpus sink) — strong; AEP-023 (sweep history), AEP-022 (trajectory capture) — soft |

> **REX** — *retour d'expérience* — the after-action write-up an SRE team
> produces once an incident is closed: what happened, what was tried, what
> actually fixed it. This AEP is about Orrery producing its own.

## Gap Analysis

### Current Implementation

The platform accumulates a great deal of operational experience and then throws
essentially all of it away:

- Every session's events persist in Postgres via `DatabaseSessionService`.
- `AuditPlugin` records every tool attempt and outcome.
- `ActivityPlugin` records per-session tool calls, bounded by
  `MAX_SESSION_LOG_ENTRIES`.
- `MemoryPlugin` saves sessions of four or more events into long-term memory.
- `record_triage_verdict` writes `incident_severity` + `triage_report` per sweep.

What is missing is any **aggregate** view across that history. Nothing asks:

- Which failure signatures recur, and how often?
- Which remediation actually resolved each class of incident — and which were
  tried first and failed?
- Where does the closed-loop remediation exhaust its 3-iteration cap, meaning a
  human always finishes the job?
- Which incidents have no runbook covering them?

The platform has, in other words, run the experiment many times and never read
the results. Meanwhile AEP-025 gives it a corpus to read from — but somebody
still has to write the documents, and the richest source of them is the
platform's own history.

### Why now

AEP-025 makes retrieval possible and AEP-017 seeds it with hand-written
runbooks, but hand-written runbooks decay: they describe the incidents someone
remembered to document, in the state the system was in that quarter. Mining the
platform's actual history closes the loop — the corpus grows from what really
happened rather than from what someone had time to write down.

### What ADK offers, and its cost

ADK ships `BigQueryAgentAnalyticsPlugin`: an async, batched Write-API sink that
streams session/event/tool telemetry to BigQuery. As of ADK 2.7 it needs the
`google-adk[bigquery-analytics]` extra — PyArrow moved out of `gcp` to shed
~50 MB from installs that do not use it.

It is a good analytics surface and a poor system of record. Adopting it as the
*only* store would put a GCP dependency in the middle of a platform whose
provider-neutrality is deliberate, and would make an offline or on-prem
deployment unable to run its own REX. The design below treats BigQuery as one
optional sink behind a seam, mirroring AEP-025's treatment of retrieval.

## Proposed Solution

A mining pass over recorded history, an LLM that drafts a REX document from each
cluster, and a **pull request** as the only path into the live corpus.

```
 sessions + audit + triage verdicts
        (Postgres, always)                       BigQuery (optional sink)
                 │                                        │
                 └──────────────► ExperienceStore ◄────────┘
                                        │
                            incident clustering (deterministic)
                                        │
                                 REX drafting (LLM)
                                        │
                              docs/rex/*.md as a PR ──► human review
                                        │
                                   merge to main
                                        │
                        make knowledge-sync (AEP-025) ──► corpus
```

### Step 1: `ExperienceStore` — one seam, two implementations

```python
class ExperienceStore(Protocol):
    async def incidents(self, since: datetime) -> list[IncidentRecord]: ...
    async def tool_outcomes(self, incident_id: str) -> list[ToolOutcome]: ...
```

- `PostgresExperienceStore` — the default, querying the session/audit tables
  the platform already writes. No new dependency, works offline, works on
  a laptop.
- `BigQueryExperienceStore` — reads what `BigQueryAgentAnalyticsPlugin` streamed,
  for deployments with enough history that SQL over columnar storage is the
  right tool.

`ORRERY_ANALYTICS_SINK=none|bigquery` gates whether the ADK plugin is registered
at all; `none` is the default and nothing in the mining path requires it.

### Step 2: Clustering is deterministic, not model-driven

An incident's signature is built from recorded facts — affected system, tool
error classes, triage severity, the remediation path taken — and clustered by
similarity. Deliberately **not** an LLM job: clustering is where a hallucination
would silently corrupt every downstream document, and "these 14 sweeps hit the
same signature" is a claim that must be reproducible from the data.

The model is used only in Step 3, to *write prose about a cluster the data
already established*.

### Step 3: REX drafting

For each cluster above a recurrence threshold, an LLM drafts a document with a
fixed structure: signature and detection, observed frequency and window, what
the agent tried in order, what actually resolved it, open questions, and a
proposed runbook section.

Every claim cites its evidence — session ids, sweep timestamps, tool outcomes.
A REX that cannot cite is a REX that gets dropped, enforced by a validator, not
by prompt instruction.

### Step 4: The human gate — the core safety property

**Generated documents never enter the live corpus automatically.** The pipeline
opens a pull request against `docs/rex/`; a human reviews and merges; the
existing `make knowledge-sync` picks it up.

This is not process ceremony, it is the thing that keeps the system sound.
Without it the loop closes on itself: the agent writes a document from its own
possibly-wrong conclusions, indexes it, retrieves it in the next incident with
the full authority of a runbook, and acts on it — then writes a new REX
confirming the pattern. Model error becomes indexed institutional fact, and each
pass makes it look better sourced. A pull request is a cheap, auditable, already
familiar circuit-breaker on that loop.

Corollary: REX documents are labelled `origin: generated` in their front matter,
so retrieval can weight them below human-authored runbooks and an operator can
always see which is which.

### Step 5: Coverage reporting

The same mining pass answers a question nobody can answer today: **which
recurring incidents have no runbook?** Cross-referencing clusters against the
AEP-025 corpus produces a gap list, which is a far better prioritisation input
for AEP-017 than intuition — it names the runbooks worth writing next, ranked by
how often the incident actually occurs.

### Step 6: Where it runs

A `make rex` target for on-demand use, and a scheduled monthly job. If AEP-023
lands, it becomes a scheduled task with persisted run history like any other;
until then a CI cron is sufficient. The pass is read-only against production
data and runs at **L2** — it inspects history and writes a PR, and must never be
able to touch infrastructure.

## Affected Files

| File | Change |
|------|--------|
| `core/orrery_core/experience/store.py` | New — `ExperienceStore` protocol + Postgres implementation |
| `core/orrery_core/experience/bigquery.py` | New — BigQuery implementation (extra-gated import) |
| `core/orrery_core/experience/clustering.py` | New — deterministic incident signatures + clustering |
| `core/orrery_core/experience/rex.py` | New — drafting, citation validator, front-matter labelling |
| `core/orrery_core/plugins/__init__.py` | Register `BigQueryAgentAnalyticsPlugin` when `ORRERY_ANALYTICS_SINK=bigquery` |
| `pyproject.toml` | Optional `bigquery` extra → `google-adk[bigquery-analytics]` |
| `scripts/run_rex.py` + `Makefile` | `make rex` — mine, draft, open PR |
| `docs/rex/` | New — generated REX documents (reviewed, merged) |
| `docs/knowledge.md` | Document the generated-vs-authored distinction |
| `core/tests/test_experience_*.py` | Clustering determinism, citation validation, PR-gate enforcement |

## Acceptance Criteria

- [ ] `ExperienceStore` protocol with a Postgres implementation that needs no cloud dependency
- [ ] BigQuery sink and store are both opt-in; `ORRERY_ANALYTICS_SINK=none` is the default and the ADK plugin is not registered
- [ ] Clustering is deterministic and reproducible from stored data — same input, same clusters, no model call
- [ ] Every REX claim cites session ids / sweep timestamps; a draft that fails the citation validator is not written
- [ ] Generated documents reach the corpus **only** through a merged pull request — enforced by test, not convention
- [ ] REX front matter carries `origin: generated`; retrieval can distinguish it from authored runbooks
- [ ] Coverage report lists recurring incident clusters with no matching runbook
- [ ] The mining pass runs at L2 and touches no infrastructure

## Notes

- **BigQuery is an analytics surface, not a system of record.** The audit trail
  (and AEP-024's approval events) must keep the same durability guarantees as
  the rest of the platform and must not depend on a cloud sink being reachable.
  Streaming a copy to BigQuery for analysis is useful; treating it as the
  authoritative store is not.
- **Retrieval quality degrades as generated content grows.** A corpus that is
  mostly machine-written drifts toward the model's own phrasing and away from
  how engineers actually describe problems, which hurts recall. The `origin`
  label exists so this can be measured; if generated documents come to dominate,
  that is a signal to tighten the recurrence threshold, not to generate more.
- Sequenced after AEP-025 because a generated REX with nowhere to be indexed is
  just a file. Running the coverage report (Step 5) early is worthwhile on its
  own, though — it can prioritise AEP-017's hand-written runbooks before any of
  the generation machinery exists.
