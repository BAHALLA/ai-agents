import type { ActivityEntry } from "../api/types";

interface Props {
  entries: ActivityEntry[];
}

/** Compact "HH:MM:SS" from an ISO timestamp; empty string when unparseable. */
function timeOf(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString();
}

/**
 * The tool-call timeline: every recorded tool execution for this session,
 * so the user sees the orchestration (which specialist ran which tool, with
 * what outcome) instead of an opaque paragraph. Collapsed by default —
 * it's supporting evidence, not the conversation.
 */
export function ToolTimeline({ entries }: Props) {
  if (entries.length === 0) return null;

  return (
    <details className="timeline">
      <summary className="timeline__summary">
        Tool calls <span className="timeline__count">{entries.length}</span>
      </summary>
      <ol className="timeline__list">
        {entries.map((entry, i) => (
          <li key={`${entry.timestamp}-${i}`} className="timeline__item">
            <span className="timeline__time">{timeOf(entry.timestamp)}</span>
            <span className="timeline__op">{entry.operation}</span>
            <span className="timeline__details">{entry.details}</span>
          </li>
        ))}
      </ol>
    </details>
  );
}
