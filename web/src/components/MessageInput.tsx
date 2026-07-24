import { useLayoutEffect, useRef, useState } from "react";

interface Props {
  disabled: boolean;
  isSending: boolean;
  onSend: (text: string) => void;
  onStop: () => void;
}

/** Tallest the composer grows before it starts scrolling internally (px). */
const MAX_HEIGHT = 160;

/**
 * Composer. Enter sends; Shift+Enter inserts a newline. Empty/whitespace input
 * is ignored. Grows with the message up to {@link MAX_HEIGHT}, so writing a
 * multi-line prompt doesn't happen through a one-line peephole.
 *
 * While a turn is in flight the Send button becomes **Stop**: a triage sweep
 * fans out to five specialists and can run for a minute, and until AEP-009
 * streaming lands there is no partial output — so the only alternative to
 * waiting it out was reloading the page.
 */
export function MessageInput({ disabled, isSending, onSend, onStop }: Props) {
  const [value, setValue] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Re-measure before paint so the box never flashes at the wrong height.
  useLayoutEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto"; // reset first, or it can only ever grow
    el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT)}px`;
  }, [value]);

  const submit = () => {
    const text = value.trim();
    if (!text || disabled) return;
    onSend(text);
    setValue("");
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <form
      className="flex items-end gap-2 border-t border-slate-200 bg-white p-3 dark:border-slate-800 dark:bg-slate-900"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <textarea
        ref={textareaRef}
        className="orrery-scroll min-h-[2.75rem] flex-1 resize-none rounded-xl border border-slate-300 bg-slate-50 px-3 py-2.5 text-sm text-slate-900 placeholder:text-slate-400 focus:border-blue-500 focus:ring-2 focus:ring-blue-500/30 focus:outline-none disabled:opacity-60 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100 dark:placeholder:text-slate-500"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder="Ask Orrery…  (Enter to send, Shift+Enter for a new line)"
        rows={1}
        disabled={disabled}
        aria-label="Message"
      />
      {isSending ? (
        <button
          type="button"
          onClick={onStop}
          className="rounded-xl border border-slate-300 px-4 py-2.5 text-sm font-medium text-slate-700 hover:bg-slate-100 dark:border-slate-600 dark:text-slate-200 dark:hover:bg-slate-800"
        >
          Stop
        </button>
      ) : (
        <button
          type="submit"
          disabled={disabled || !value.trim()}
          className="rounded-xl bg-blue-600 px-4 py-2.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
        >
          Send
        </button>
      )}
    </form>
  );
}
