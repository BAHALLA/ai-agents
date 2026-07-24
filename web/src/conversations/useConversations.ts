import { useCallback, useEffect, useMemo, useReducer, useRef } from "react";
import type { ChatMessage } from "../chat/types";
import { storageKeys } from "../config";
import { NEW_CONVERSATION_TITLE, type Conversation } from "./types";

/** Cap on stored conversations so localStorage can't grow without bound and
 * silently start failing to persist. The least-recently-updated drop first —
 * the same order the sidebar shows, so what disappears is never a surprise. */
const MAX_CONVERSATIONS = 50;

/** Cap on messages persisted per conversation. The conversation count alone
 * does not bound storage: one long-running incident thread grows forever. The
 * live in-memory transcript is never trimmed — only what gets written. */
const MAX_PERSISTED_MESSAGES = 200;

let seq = 0;
function newId(): string {
  seq += 1;
  return `c-${Date.now()}-${seq}`;
}

function emptyConversation(): Conversation {
  return {
    id: newId(),
    sessionId: null,
    title: NEW_CONVERSATION_TITLE,
    messages: [],
    updatedAt: Date.now(),
  };
}

function isMessage(v: unknown): v is ChatMessage {
  const m = v as ChatMessage | null;
  return (
    !!m &&
    typeof m.id === "string" &&
    (m.role === "user" || m.role === "assistant") &&
    typeof m.text === "string" &&
    typeof m.at === "number"
  );
}

/** A stored conversation is only trusted if every field matches the current
 * shape — a corrupt or old-schema entry is dropped rather than allowed to
 * throw later during render (e.g. reading `.messages.length`). */
function isConversation(v: unknown): v is Conversation {
  const c = v as Conversation | null;
  return (
    !!c &&
    typeof c.id === "string" &&
    (c.sessionId === null || typeof c.sessionId === "string") &&
    typeof c.title === "string" &&
    typeof c.updatedAt === "number" &&
    Array.isArray(c.messages) &&
    c.messages.every(isMessage)
  );
}

/** Keep the {@link MAX_CONVERSATIONS} most-recently-updated conversations. */
function evict(list: Conversation[]): Conversation[] {
  if (list.length <= MAX_CONVERSATIONS) return list;
  const keep = new Set(
    [...list]
      .sort((a, b) => b.updatedAt - a.updatedAt)
      .slice(0, MAX_CONVERSATIONS)
      .map((c) => c.id),
  );
  return list.filter((c) => keep.has(c.id));
}

function load(): ConversationsState {
  try {
    const raw = localStorage.getItem(storageKeys.conversations);
    const parsed: unknown = raw ? JSON.parse(raw) : null;
    const list = evict(Array.isArray(parsed) ? parsed.filter(isConversation) : []);
    if (list.length > 0) {
      const stored = localStorage.getItem(storageKeys.activeConversation);
      const activeId = list.some((c) => c.id === stored) ? stored! : list[0].id;
      return { list, activeId };
    }
  } catch {
    // fall through to a fresh conversation
  }
  const fresh = emptyConversation();
  return { list: [fresh], activeId: fresh.id };
}

/** Write the history back, shedding load rather than failing silently.
 *
 * A browser that refuses the write (quota, private mode) would otherwise leave
 * the operator with a console that looks like it is saving and is not, so a
 * rejected write retries against progressively smaller slices before giving up.
 */
function persist(state: ConversationsState): void {
  const trimmed = state.list.map((c) =>
    c.messages.length > MAX_PERSISTED_MESSAGES
      ? { ...c, messages: c.messages.slice(-MAX_PERSISTED_MESSAGES) }
      : c,
  );
  const byRecency = [...trimmed].sort((a, b) => b.updatedAt - a.updatedAt);
  for (const size of [byRecency.length, 10, 1]) {
    try {
      localStorage.setItem(storageKeys.conversations, JSON.stringify(byRecency.slice(0, size)));
      localStorage.setItem(storageKeys.activeConversation, state.activeId);
      return;
    } catch {
      // quota or storage unavailable — retry with fewer conversations
    }
  }
}

interface ConversationsState {
  list: Conversation[];
  activeId: string;
}

type ConversationPatch =
  | Partial<Omit<Conversation, "id">>
  | ((current: Conversation) => Partial<Omit<Conversation, "id">>);

/** Every mutation carries what it needs, so the reducer stays a pure function
 * of (state, action) — no `Date.now()`, no id generation, nothing that would
 * differ between StrictMode's two invocations. */
type ConversationsAction =
  | { type: "new"; fresh: Conversation }
  | { type: "select"; id: string }
  | { type: "delete"; id: string; fallback: Conversation }
  | { type: "patch"; patch: ConversationPatch; at: number };

/**
 * The list and the active selection move together, in one reducer.
 *
 * They are a single piece of state: creating, deleting, and selecting all
 * change both at once. Held as two `useState`s they could only be updated by
 * either reading the list from the render closure (a stale snapshot — an
 * update queued earlier in the same tick, such as an arriving assistant reply,
 * gets overwritten) or by calling `setActiveId` inside the list updater (an
 * impure reducer, double-invoked under StrictMode). A reducer has neither
 * problem: React replays every dispatch against the freshest state.
 */
function reducer(state: ConversationsState, action: ConversationsAction): ConversationsState {
  switch (action.type) {
    case "new": {
      // Reuse an existing empty conversation instead of piling up blanks.
      const blank = state.list.find((c) => c.messages.length === 0);
      if (blank) return { ...state, activeId: blank.id };
      return { list: evict([action.fresh, ...state.list]), activeId: action.fresh.id };
    }
    case "select":
      return state.activeId === action.id ? state : { ...state, activeId: action.id };
    case "delete": {
      const next = state.list.filter((c) => c.id !== action.id);
      if (next.length === 0) return { list: [action.fallback], activeId: action.fallback.id };
      return {
        list: next,
        activeId: state.activeId === action.id ? next[0].id : state.activeId,
      };
    }
    case "patch": {
      const list = state.list.map((c) => {
        if (c.id !== state.activeId) return c;
        const delta = typeof action.patch === "function" ? action.patch(c) : action.patch;
        return { ...c, ...delta, updatedAt: action.at };
      });
      return { ...state, list };
    }
  }
}

export interface ConversationsController {
  /** Newest first. */
  conversations: Conversation[];
  activeId: string;
  active: Conversation;
  newConversation: () => void;
  selectConversation: (id: string) => void;
  deleteConversation: (id: string) => void;
  /** Merge a partial update into the active conversation (bumps updatedAt).
   * Accepts an updater so async callers always read the freshest transcript. */
  patchActive: (patch: ConversationPatch) => void;
}

/**
 * Owns the conversation history (list + active selection), persisted to
 * localStorage. The active conversation is the source of truth for the
 * transcript and its server session id; {@link useChat} drives it.
 */
export function useConversations(): ConversationsController {
  const initial = useRef(load());
  const [state, dispatch] = useReducer(reducer, initial.current);
  const { list, activeId } = state;

  useEffect(() => persist(state), [state]);

  const active = useMemo(() => list.find((c) => c.id === activeId) ?? list[0], [list, activeId]);

  const newConversation = useCallback(
    () => dispatch({ type: "new", fresh: emptyConversation() }),
    [],
  );

  const selectConversation = useCallback((id: string) => dispatch({ type: "select", id }), []);

  const deleteConversation = useCallback(
    (id: string) => dispatch({ type: "delete", id, fallback: emptyConversation() }),
    [],
  );

  const patchActive = useCallback(
    (patch: ConversationPatch) => dispatch({ type: "patch", patch, at: Date.now() }),
    [],
  );

  const sorted = useMemo(() => [...list].sort((a, b) => b.updatedAt - a.updatedAt), [list]);

  return {
    conversations: sorted,
    activeId,
    active,
    newConversation,
    selectConversation,
    deleteConversation,
    patchActive,
  };
}
