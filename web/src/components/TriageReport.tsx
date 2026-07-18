import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { TriageResponse } from "../api/types";
import { severityAccent, severityBadge } from "./severity";

interface Props {
  triage: TriageResponse;
}

/**
 * The session's latest triage verdict: a severity header with the full report
 * rendered as prose. The verdict comes from `record_triage_verdict` (session
 * state), not from parsing the chat transcript, so the badge always reflects
 * the recorded machine-readable severity.
 */
export function TriageReport({ triage }: Props) {
  if (!triage.severity) {
    return (
      <p className="p-4 text-sm text-slate-500 dark:text-slate-400">
        No triage verdict yet. Click <span className="font-medium">Run triage</span> to sweep every
        system.
      </p>
    );
  }

  return (
    <div className={`border-l-4 ${severityAccent[triage.severity]}`}>
      <div className="flex items-center gap-2 px-4 py-3">
        <span
          className={`rounded-full px-2.5 py-0.5 text-xs font-semibold uppercase tracking-wide ${severityBadge[triage.severity]}`}
        >
          {triage.severity}
        </span>
        <span className="text-sm font-medium text-slate-700 dark:text-slate-200">
          Latest triage verdict
        </span>
      </div>
      {triage.report ? (
        <div className="prose prose-sm dark:prose-invert max-w-none px-4 pb-4">
          <Markdown remarkPlugins={[remarkGfm]}>{triage.report}</Markdown>
        </div>
      ) : null}
    </div>
  );
}
