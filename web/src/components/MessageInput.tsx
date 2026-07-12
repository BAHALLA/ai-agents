import { useState } from "react";

interface Props {
  disabled: boolean;
  onSend: (text: string) => void;
}

/**
 * Composer. Enter sends; Shift+Enter inserts a newline. Empty/whitespace input
 * is ignored. The parent controls `disabled` while a turn is in flight.
 */
export function MessageInput({ disabled, onSend }: Props) {
  const [value, setValue] = useState("");

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
      className="composer"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <textarea
        className="composer__input"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder="Ask Orrery…  (Enter to send, Shift+Enter for a new line)"
        rows={1}
        disabled={disabled}
        aria-label="Message"
      />
      <button type="submit" className="btn btn--primary" disabled={disabled || !value.trim()}>
        Send
      </button>
    </form>
  );
}
