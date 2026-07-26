import type { ChatMessage } from "../chat/types";

/** One conversation in the sidebar history.
 *
 * The session store behind `GET /sessions` is the source of truth — the browser
 * keeps no copy, so history follows the user to any machine. What lives here is
 * the render state for one entry in that list.
 *
 * `id` stays a client-side identity distinct from `sessionId`: a conversation
 * exists in the sidebar before its first turn is sent, which is the only moment
 * the server issues a session id. Keeping the two separate means an entry's
 * identity never changes underneath an in-flight request.
 */
export interface Conversation {
  id: string;
  /** Server-issued session id; null for a draft whose first turn hasn't landed. */
  sessionId: string | null;
  /** From the server for a stored conversation; "New chat" while untitled. */
  title: string;
  messages: ChatMessage[];
  /** Epoch ms of the last activity (the server's `last_update_time`, scaled). */
  updatedAt: number;
  /**
   * Whether `messages` is the real transcript.
   *
   * `GET /sessions` returns titles without transcripts, so a listed
   * conversation starts empty and unloaded and opening it fetches the messages.
   * Without this flag an unfetched conversation is indistinguishable from a
   * genuinely empty one, and the sidebar would call every stored thread "empty".
   */
  loaded: boolean;
}

export const NEW_CONVERSATION_TITLE = "New chat";
