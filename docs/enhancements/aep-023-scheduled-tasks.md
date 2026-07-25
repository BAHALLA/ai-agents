# AEP-023: First-Class Scheduled Agent Tasks

| Field | Value |
|-------|-------|
| **Status** | <span class="badge badge--amber">proposed</span> |
| **Priority** | <span class="badge badge--blue">P2</span> |
| **Effort** | Medium (3-5 days) |
| **Impact** | Medium |
| **Dependencies** | AEP-011 (Postgres persistence) — strong; AEP-018 (HPA/multi-replica) — soft |

> Pattern borrowed from the Hermes agent architecture (first-class cron scheduling
> with JSON-persisted job state). Retargeted at Orrery's deterministic triage
> Workflow.

## Gap Analysis

### Current Implementation

The deterministic incident-triage Workflow exists and is designed for batch use:

- `agents/orrery-assistant/run_triage.py` runs `orrery_triage_workflow` **once**
  ("Intended for cron / CI / on-call sweeps") and exits.
- Scheduling, run history, and failure handling all live **outside** the app —
  you'd wrap the script in a host cron / Kubernetes `CronJob` and pipe logs
  somewhere yourself.

There is no first-class notion of a **schedule** or a **run history**:

- No persisted record of "run a full sweep every 15 minutes."
- No queryable history of past sweeps (verdict, severity, duration, trend).
- No in-app visibility — the web console (AEP-019) can trigger a sweep on demand
  but can't show "here's what the 02:00 sweep found."

### Why this matters now

For an SRE platform, *proactive* recurring triage is the natural mode — catch a
degradation at 02:00 before a human is paged, and show the **trend** of verdicts
over time, not just the latest one. The Workflow is already built (AEP-003/004
graph inversion); what's missing is the thin scheduling + history layer around
it. AEP-011 already gives a Postgres store to persist to, and AEP-019 gives a UI
that would immediately benefit from a sweep history pane.

### What's available

- `orrery_triage_workflow` is a ready-to-run batch root; `record_triage_verdict`
  already writes `incident_severity` + `triage_report` to state.
- `DATABASE_URL` + the persistence layer (`core/orrery_core/persistence/db.py`)
  already back sessions/memory/confirmations — a `scheduled_runs` table is a small
  addition using the same engine.
- The Kubernetes deployment can run a dedicated scheduler replica; APScheduler (or
  a DB-backed leader-elected loop) covers in-process scheduling.

## Proposed Solution

A persisted schedule + a scheduler that runs the triage Workflow on cadence and
records each run, exposed read-only through the existing HTTP/web surface.

### Step 1: Persisted schedule + run history

```sql
CREATE TABLE triage_schedules (
    id            TEXT PRIMARY KEY,
    cron          TEXT NOT NULL,            -- "*/15 * * * *"
    enabled       BOOLEAN NOT NULL DEFAULT true,
    autonomy_level TEXT NOT NULL DEFAULT 'L2',  -- sweeps stay read-only by default
    created_by    TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE triage_runs (
    id            TEXT PRIMARY KEY,
    schedule_id   TEXT REFERENCES triage_schedules(id),
    started_at    TIMESTAMPTZ NOT NULL,
    finished_at   TIMESTAMPTZ,
    severity      TEXT,                     -- healthy | degraded | critical
    report        TEXT,
    status        TEXT NOT NULL             -- running | ok | error
);
```

### Step 2: Scheduler process (one leader across replicas)

Add `agents/orrery-assistant/scheduler.py`:

```python
async def run_due_schedules(store: ScheduleStore):
    for sched in await store.due_now():
        run_id = await store.start_run(sched.id)
        try:
            state = await run_triage_workflow(autonomy_level=sched.autonomy_level)
            await store.finish_run(
                run_id,
                severity=state.get("incident_severity"),
                report=state.get("triage_report"),
                status="ok",
            )
        except Exception as exc:
            await store.finish_run(run_id, status="error", report=str(exc))
            logger.exception("scheduled_triage_failed", extra={"schedule_id": sched.id})
```

**Multi-replica safety** (AEP-018 territory): guard with a Postgres advisory lock
or a `SELECT … FOR UPDATE SKIP LOCKED` claim so exactly one replica runs a due
schedule — mirroring the confirmation store's atomic-claim pattern. When the
backend is in-memory, refuse to run more than one scheduler replica (same guard
the Pub/Sub worker uses).

### Step 3: Read-only API + web history pane

```
GET  /triage/schedules            → list schedules (admin)
POST /triage/schedules            → create (admin; cron + autonomy validated)
GET  /triage/runs?limit=50        → recent runs (verdict + severity + duration)
GET  /triage/runs/{id}            → full report for one run
```

Reuse the AEP-013 auth perimeter (create/modify = admin; read = operator+). The
web console (AEP-019) gains a **"Sweep history"** pane: a sparkline of severity
over time plus the latest report — turning point-in-time triage into a trend.

### Step 4: Deployment

- A dedicated `scheduler` Deployment (1 replica, or N with leader election) in the
  Helm chart, sharing the app image and `DATABASE_URL`.
- Sweeps run at **L2 (read-only)** autonomy by default — a scheduled sweep must
  never silently mutate infrastructure. Any remediation stays gated behind the
  normal L4 + confirmation flow and thus can't fire unattended.

## Affected Files

| File | Change |
|------|--------|
| `core/orrery_core/persistence/schedules.py` | New — `ScheduleStore` (memory + Postgres), atomic run-claim |
| `agents/orrery-assistant/scheduler.py` | New — scheduler loop over the triage Workflow |
| `core/orrery_core/serving/server.py` | Add `/triage/schedules` + `/triage/runs` routes (auth-gated) |
| `deploy/k8s/` + Helm chart | New `scheduler` Deployment; single-replica guard for memory backend |
| `web/` | New "Sweep history" pane (severity sparkline + latest report) |
| `core/tests/test_schedules.py` | New — cron matching, atomic claim, run recording, L2 default |
| `docs/agents-overview.md` / `docs/deployment.md` | Document scheduled sweeps |

## Acceptance Criteria

- [ ] `triage_schedules` + `triage_runs` persisted (Postgres; in-memory for dev)
- [ ] Scheduler runs `orrery_triage_workflow` on cron cadence and records each run
- [ ] Exactly-once execution across replicas via atomic claim (advisory lock / `SKIP LOCKED`)
- [ ] In-memory backend refuses >1 scheduler replica (mirrors Pub/Sub worker guard)
- [ ] Scheduled sweeps run at **L2 read-only** by default; no unattended mutation
- [ ] Read-only `/triage/runs` history behind the AEP-013 auth perimeter
- [ ] Web console shows a sweep-history pane (severity trend + latest report)
- [ ] Unit tests: cron matching, atomic claim under contention, verdict recording

## Notes

- The **safety invariant is the headline**: an unattended, scheduled agent must be
  read-only by construction. Reuse `AutonomyPlugin` L2 rather than trusting the
  prompt — a scheduled sweep that could remediate on its own is a foot-gun.
- History unlocks trend analysis the point-in-time verdict can't give: "degraded
  for the last 4 sweeps" is a stronger signal than one red banner.
- Keep the scheduler dumb — it runs the *existing* Workflow. All triage logic
  stays in `orrery_triage_workflow`; this AEP adds only cadence + persistence + a
  read surface.
