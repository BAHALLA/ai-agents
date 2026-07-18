import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { TriageResponse } from "../api/types";

interface Props {
  triage: TriageResponse;
}

/**
 * The session's latest triage verdict: a severity badge with the full report
 * collapsed underneath. The verdict comes from `record_triage_verdict`
 * (session state), not from parsing the chat transcript — the badge always
 * reflects the recorded machine-readable severity.
 */
export function TriageBanner({ triage }: Props) {
  if (!triage.severity) return null;

  return (
    <details className={`triage triage--${triage.severity}`}>
      <summary className="triage__summary">
        <span className="triage__badge">{triage.severity}</span>
        <span className="triage__label">Last triage verdict — view report</span>
      </summary>
      {triage.report ? (
        <div className="triage__report bubble__text--md">
          <Markdown remarkPlugins={[remarkGfm]}>{triage.report}</Markdown>
        </div>
      ) : null}
    </details>
  );
}
