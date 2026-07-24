import type { CheckResult } from "../api/types";
import type { SystemController } from "../system/useSystem";

interface Props {
  system: SystemController;
}

const autonomyHelp: Record<string, string> = {
  L2: "Read-only. Every mutating tool is blocked, whatever your role.",
  L3: "Mutating tools run; destructive ones are blocked.",
  L4: "Destructive tools run after an explicit human confirmation.",
};

function CheckRow({ check }: { check: CheckResult }) {
  return (
    <li className="flex gap-2.5 border-b border-slate-200 px-4 py-3 last:border-0 dark:border-slate-800">
      <span aria-hidden="true" className="pt-0.5 text-sm">
        {check.ok ? "🟢" : "🔴"}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline justify-between gap-2">
          <span className="truncate text-sm font-medium text-slate-800 dark:text-slate-100">
            {check.label}
          </span>
          <span className="shrink-0 text-xs tabular-nums text-slate-400 dark:text-slate-500">
            {check.duration_ms}ms
          </span>
        </div>
        <p
          className={`mt-0.5 text-xs break-words ${
            check.ok ? "text-slate-500 dark:text-slate-400" : "text-red-700 dark:text-red-300"
          }`}
        >
          {check.detail}
        </p>
        {check.hint ? (
          <p className="mt-1 rounded bg-amber-50 px-2 py-1 text-xs text-amber-800 dark:bg-amber-950/40 dark:text-amber-200">
            {check.hint}
          </p>
        ) : null}
      </div>
    </li>
  );
}

/**
 * Deployment posture and the first-run environment check (AEP-019 Milestone 3).
 *
 * The point of this panel is to answer "is anything actually wired?" without
 * the user having to ask the agent and interpret a stack trace buried in a tool
 * result — which is how nearly every first-run failure presents today.
 */
export function SystemPanel({ system }: Props) {
  const { me, selfTest, isChecking, error, runSelfTest } = system;

  return (
    <div className="flex flex-col">
      <dl className="border-b border-slate-200 px-4 py-3 text-sm dark:border-slate-800">
        <div className="flex justify-between gap-2 py-0.5">
          <dt className="text-slate-500 dark:text-slate-400">Role (server)</dt>
          <dd className="font-medium text-slate-800 dark:text-slate-100">{me ? me.role : "—"}</dd>
        </div>
        <div className="flex justify-between gap-2 py-0.5">
          <dt className="text-slate-500 dark:text-slate-400">Autonomy</dt>
          <dd
            className="font-medium text-slate-800 dark:text-slate-100"
            title={me?.autonomy_level ? autonomyHelp[me.autonomy_level] : undefined}
          >
            {me?.autonomy_level ?? "not enforced"}
          </dd>
        </div>
        <div className="flex justify-between gap-2 py-0.5">
          <dt className="text-slate-500 dark:text-slate-400">Model</dt>
          <dd className="min-w-0 truncate font-medium text-slate-800 dark:text-slate-100">
            {me ? `${me.model_provider}${me.model_name ? ` / ${me.model_name}` : ""}` : "—"}
          </dd>
        </div>
      </dl>

      {me?.autonomy_level && autonomyHelp[me.autonomy_level] ? (
        <p className="border-b border-slate-200 bg-slate-100 px-4 py-2 text-xs text-slate-600 dark:border-slate-800 dark:bg-slate-800/50 dark:text-slate-300">
          {autonomyHelp[me.autonomy_level]}
        </p>
      ) : null}

      <div className="p-4">
        <button
          type="button"
          onClick={() => void runSelfTest()}
          disabled={isChecking || me?.self_test_available === false}
          className="w-full rounded-lg bg-blue-600 px-3 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
        >
          {isChecking ? "Checking…" : selfTest ? "Re-run checks" : "Run checks"}
        </button>
        {me?.self_test_available === false ? (
          <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
            This deployment registered no integration probes.
          </p>
        ) : (
          <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
            Read-only. Reaches each integration once and does a one-token model round-trip.
          </p>
        )}
      </div>

      {error ? (
        <p
          role="alert"
          className="mx-4 mb-3 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950/50 dark:text-red-300"
        >
          {error}
        </p>
      ) : null}

      {selfTest ? (
        <>
          <p
            className={`px-4 pb-2 text-sm font-medium ${
              selfTest.ok
                ? "text-green-700 dark:text-green-300"
                : "text-amber-700 dark:text-amber-300"
            }`}
            role="status"
          >
            {selfTest.ok
              ? "All checks passed."
              : `${selfTest.checks.filter((c) => !c.ok).length} of ${selfTest.checks.length} checks failed.`}
          </p>
          <ul className="border-t border-slate-200 dark:border-slate-800">
            {selfTest.checks.map((check) => (
              <CheckRow key={check.name} check={check} />
            ))}
          </ul>
        </>
      ) : null}
    </div>
  );
}
