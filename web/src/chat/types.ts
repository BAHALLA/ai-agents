/** A single message rendered in the conversation. */
export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  at: number;
}

/** Non-terminal, per-message error surfaced inline in the transcript. */
export interface ChatError {
  message: string;
  /** True when re-authenticating would likely fix it. */
  isAuth: boolean;
}
