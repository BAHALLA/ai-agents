import type { ActivityEntry } from "../api/types";

/** Per-specialist rollup of what a sweep actually did. */
export interface SpecialistStatus {
  /** Stable key — the specialist's agent name family. */
  key: string;
  label: string;
  calls: number;
  failures: number;
  /** "ok" when every call succeeded, "failed" when every one failed. */
  state: "ok" | "partial" | "failed";
}

/**
 * Agent-name prefixes → the system they speak to. Both the chat specialists
 * (`kafka_health_agent`) and the triage workflow's checker nodes
 * (`kafka_health_checker`) map to the same row, since to an operator they are
 * the same system being asked the same question.
 */
const SPECIALISTS: ReadonlyArray<{ key: string; label: string; match: RegExp }> = [
  { key: "kafka", label: "Kafka", match: /^kafka_/ },
  { key: "k8s", label: "Kubernetes", match: /^k8s_/ },
  { key: "elasticsearch", label: "Elasticsearch", match: /^elasticsearch_/ },
  { key: "observability", label: "Observability", match: /^observability_/ },
  { key: "docker", label: "Docker", match: /^docker_/ },
];

/** Statuses a tool result reports when the call did not succeed. */
const FAILURE_STATUSES = new Set(["error", "access_denied", "blocked", "denied", "circuit_open"]);

/** Pull `[agent]` and the trailing `→ status` back out of an activity detail. */
function parseDetail(entry: ActivityEntry): { agent: string; status: string } {
  const agent = /^\[([^\]]+)\]/.exec(entry.details)?.[1] ?? "";
  const arrow = entry.details.lastIndexOf("→");
  const status =
    arrow === -1
      ? ""
      : entry.details
          .slice(arrow + 1)
          .trim()
          .toLowerCase();
  return { agent, status };
}

/**
 * Roll the tool-call timeline up into one row per specialist.
 *
 * Derived from recorded tool calls rather than parsed out of the model's prose:
 * the report is free text that changes wording run to run, while the activity
 * log is structured and is what actually happened. A specialist that was never
 * called simply does not appear — "we didn't ask" is not the same as "healthy".
 */
export function specialistStatuses(entries: ActivityEntry[]): SpecialistStatus[] {
  const seen = new Map<string, SpecialistStatus>();

  for (const entry of entries) {
    const { agent, status } = parseDetail(entry);
    const specialist = SPECIALISTS.find((s) => s.match.test(agent));
    if (!specialist) continue;

    const row = seen.get(specialist.key) ?? {
      key: specialist.key,
      label: specialist.label,
      calls: 0,
      failures: 0,
      state: "ok" as const,
    };
    row.calls += 1;
    if (FAILURE_STATUSES.has(status)) row.failures += 1;
    seen.set(specialist.key, row);
  }

  // Keep the declared order so the chips don't reshuffle between refreshes.
  return SPECIALISTS.flatMap((s) => {
    const row = seen.get(s.key);
    if (!row) return [];
    const state = row.failures === 0 ? "ok" : row.failures === row.calls ? "failed" : "partial";
    return [{ ...row, state }];
  });
}
