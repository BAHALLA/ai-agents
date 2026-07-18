import type { ActivityEntry } from "../api/types";
import { statusPill } from "./severity";

interface Props {
  entries: ActivityEntry[];
}

interface ParsedCall {
  time: string;
  tool: string;
  agent: string;
  detail: string;
  status: string;
}

/** ActivityPlugin writes details as "[agent] argsummary → status". Parse it
 * back into columns, degrading gracefully if the shape ever changes. */
function parse(entry: ActivityEntry): ParsedCall {
  let agent = "";
  let rest = entry.details;
  const agentMatch = /^\[([^\]]+)\]\s*/.exec(rest);
  if (agentMatch) {
    agent = agentMatch[1];
    rest = rest.slice(agentMatch[0].length);
  }
  let status = "";
  const arrow = rest.lastIndexOf("→");
  if (arrow !== -1) {
    status = rest.slice(arrow + 1).trim();
    rest = rest.slice(0, arrow).trim();
  }
  const date = new Date(entry.timestamp);
  const time = Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString();
  return { time, tool: entry.operation, agent, detail: rest, status };
}

/** The tool-call timeline as a compact, scannable table. */
export function ToolCallsTable({ entries }: Props) {
  if (entries.length === 0) {
    return (
      <p className="p-4 text-sm text-slate-500 dark:text-slate-400">
        No tool calls yet. Ask a question or run a triage.
      </p>
    );
  }

  const rows = entries.map(parse);

  return (
    <div className="orrery-scroll overflow-auto">
      <table className="w-full border-collapse text-left text-xs">
        <thead className="sticky top-0 bg-slate-50 dark:bg-slate-900">
          <tr className="text-slate-500 dark:text-slate-400">
            <th className="px-3 py-2 font-medium">Time</th>
            <th className="px-3 py-2 font-medium">Tool</th>
            <th className="px-3 py-2 font-medium">Agent</th>
            <th className="px-3 py-2 font-medium">Status</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr
              key={`${r.time}-${i}`}
              className="border-t border-slate-100 align-top dark:border-slate-800"
            >
              <td className="whitespace-nowrap px-3 py-2 tabular-nums text-slate-500 dark:text-slate-400">
                {r.time}
              </td>
              <td className="px-3 py-2">
                <div className="font-mono font-medium text-slate-800 dark:text-slate-200">
                  {r.tool}
                </div>
                {r.detail ? (
                  <div className="mt-0.5 font-mono text-[11px] break-all text-slate-500 dark:text-slate-400">
                    {r.detail}
                  </div>
                ) : null}
              </td>
              <td className="px-3 py-2 whitespace-nowrap text-slate-600 dark:text-slate-300">
                {r.agent}
              </td>
              <td className="px-3 py-2">
                {r.status ? (
                  <span
                    className={`inline-block rounded-full px-2 py-0.5 text-[11px] font-medium ${statusPill(
                      r.status,
                    )}`}
                  >
                    {r.status}
                  </span>
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
