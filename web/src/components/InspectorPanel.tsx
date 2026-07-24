import type { ActivityEntry, TriageResponse } from "../api/types";
import type { SystemController } from "../system/useSystem";
import { SystemPanel } from "./SystemPanel";
import { ToolCallsTable } from "./ToolCallsTable";
import { TriageReport } from "./TriageReport";

export type InspectorTab = "tools" | "triage" | "system";

interface Props {
  tab: InspectorTab;
  onTab: (tab: InspectorTab) => void;
  onClose: () => void;
  activity: ActivityEntry[];
  triage: TriageResponse | null;
  system: SystemController;
}

/** Right-hand inspector: the tool-call table and the triage report, kept out
 * of the chat column so the conversation stays clean. */
export function InspectorPanel({ tab, onTab, onClose, activity, triage, system }: Props) {
  const tabClass = (active: boolean) =>
    `px-3 py-2 text-sm font-medium border-b-2 -mb-px transition-colors ${
      active
        ? "border-blue-600 text-blue-600 dark:text-blue-400 dark:border-blue-400"
        : "border-transparent text-slate-500 hover:text-slate-800 dark:text-slate-400 dark:hover:text-slate-200"
    }`;

  return (
    <aside className="flex w-96 shrink-0 flex-col border-l border-slate-200 bg-slate-50 dark:border-slate-800 dark:bg-slate-900">
      <div className="flex items-center justify-between border-b border-slate-200 px-2 dark:border-slate-800">
        <div className="flex" role="tablist">
          <button
            type="button"
            role="tab"
            aria-selected={tab === "tools"}
            className={tabClass(tab === "tools")}
            onClick={() => onTab("tools")}
          >
            Tool calls
            {activity.length > 0 ? (
              <span className="ml-1.5 rounded-full bg-slate-200 px-1.5 text-xs tabular-nums text-slate-600 dark:bg-slate-700 dark:text-slate-300">
                {activity.length}
              </span>
            ) : null}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "triage"}
            className={tabClass(tab === "triage")}
            onClick={() => onTab("triage")}
          >
            Triage
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "system"}
            className={tabClass(tab === "system")}
            onClick={() => onTab("system")}
          >
            System
            {system.selfTest && !system.selfTest.ok ? (
              <span
                className="ml-1.5 rounded-full bg-red-100 px-1.5 text-xs font-semibold text-red-700 dark:bg-red-950 dark:text-red-300"
                title="Some environment checks failed"
              >
                !
              </span>
            ) : null}
          </button>
        </div>
        <button
          type="button"
          aria-label="Close inspector"
          className="rounded p-1.5 text-slate-400 hover:bg-slate-200 hover:text-slate-700 dark:hover:bg-slate-800 dark:hover:text-slate-200"
          onClick={onClose}
        >
          ✕
        </button>
      </div>
      <div className="orrery-scroll min-h-0 flex-1 overflow-y-auto">
        {tab === "tools" ? (
          <ToolCallsTable entries={activity} />
        ) : tab === "system" ? (
          <SystemPanel system={system} />
        ) : triage ? (
          <TriageReport triage={triage} activity={activity} />
        ) : (
          <p className="p-4 text-sm text-slate-500 dark:text-slate-400">
            No triage verdict yet. Click <span className="font-medium">Run triage</span> in the
            sidebar.
          </p>
        )}
      </div>
    </aside>
  );
}
