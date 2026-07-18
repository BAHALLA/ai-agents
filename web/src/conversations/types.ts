import type { ChatMessage } from "../chat/types";

/** One conversation in the sidebar history, persisted client-side.
 *
 * The server has no "list my sessions" endpoint, so history is kept in the
 * browser: the full transcript plus the server-issued `sessionId` that threads
 * turns and keys the activity/triage lookups. */
export interface Conversation {
  id: string;
  /** Server-issued session id; null until the first turn completes. */
  sessionId: string | null;
  /** Derived from the first user message; "New chat" until then. */
  title: string;
  messages: ChatMessage[];
  updatedAt: number;
}

export const NEW_CONVERSATION_TITLE = "New chat";
