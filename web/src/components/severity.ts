import type { TriageSeverity } from "../api/types";

/** Tailwind classes for a severity badge/pill, per verdict. */
export const severityBadge: Record<TriageSeverity, string> = {
  healthy: "bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300",
  degraded: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
  critical: "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300",
};

/** Left-border accent for the triage panel, per verdict. */
export const severityAccent: Record<TriageSeverity, string> = {
  healthy: "border-l-emerald-500",
  degraded: "border-l-amber-500",
  critical: "border-l-red-500",
};

/** Classify a free-text tool-call status into a pill style. */
export function statusPill(status: string): string {
  const s = status.toLowerCase();
  if (s === "success" || s === "ok" || s === "healthy") {
    return "bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300";
  }
  if (s.includes("error") || s.includes("fail") || s === "critical") {
    return "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300";
  }
  return "bg-slate-200 text-slate-600 dark:bg-slate-700 dark:text-slate-300";
}
