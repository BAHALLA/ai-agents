import { useCallback, useEffect, useRef, useState } from "react";

interface Props {
  /** Text placed on the clipboard. */
  value: string;
  /** Accessible label; also the tooltip. */
  label?: string;
  className?: string;
}

/**
 * Copy-to-clipboard with a short "Copied" acknowledgement.
 *
 * `navigator.clipboard` is unavailable on insecure origins (a console reached
 * over plain HTTP on a bastion host, say), so it falls back to a hidden
 * textarea + `execCommand` rather than failing silently — the operator is
 * usually copying an error string precisely because they need to paste it
 * somewhere else.
 */
export function CopyButton({ value, label = "Copy", className = "" }: Props) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => (timer.current ? clearTimeout(timer.current) : undefined), []);

  const copy = useCallback(async () => {
    let ok: boolean;
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(value);
        ok = true;
      } else {
        ok = legacyCopy(value);
      }
    } catch {
      ok = legacyCopy(value);
    }
    setState(ok ? "copied" : "failed");
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setState("idle"), 1500);
  }, [value]);

  return (
    <button
      type="button"
      onClick={() => void copy()}
      title={label}
      aria-label={label}
      className={`rounded px-1.5 py-0.5 text-xs text-slate-500 transition hover:bg-slate-200 hover:text-slate-800 focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:outline-none dark:text-slate-400 dark:hover:bg-slate-700 dark:hover:text-slate-100 ${className}`}
    >
      <span aria-hidden="true">
        {state === "copied" ? "✓ Copied" : state === "failed" ? "Copy failed" : "Copy"}
      </span>
      <span className="sr-only" role="status">
        {state === "copied" ? "Copied to clipboard" : state === "failed" ? "Copy failed" : ""}
      </span>
    </button>
  );
}

/** Clipboard write for insecure origins, where navigator.clipboard is absent. */
function legacyCopy(value: string): boolean {
  try {
    const area = document.createElement("textarea");
    area.value = value;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(area);
    return ok;
  } catch {
    return false;
  }
}
