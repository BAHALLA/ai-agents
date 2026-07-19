import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ChatMessage } from "../chat/types";
import { storageKeys } from "../config";
import { NEW_CONVERSATION_TITLE, type Conversation } from "./types";

/** Cap on stored conversations so localStorage can't grow without bound and
 * silently start failing to persist. Oldest (least-recently-inserted) drop. */
const MAX_CONVERSATIONS = 50;

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

function load(): { list: Conversation[]; activeId: string } {
  try {
    const raw = localStorage.getItem(storageKeys.conversations);
    const parsed: unknown = raw ? JSON.parse(raw) : null;
    const list = (Array.isArray(parsed) ? parsed.filter(isConversation) : []).slice(
      0,
      MAX_CONVERSATIONS,
    );
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
  patchActive: (
    patch:
      | Partial<Omit<Conversation, "id">>
      | ((current: Conversation) => Partial<Omit<Conversation, "id">>),
  ) => void;
}

/**
 * Owns the conversation history (list + active selection), persisted to
 * localStorage. The active conversation is the source of truth for the
 * transcript and its server session id; {@link useChat} drives it.
 */
export function useConversations(): ConversationsController {
  const initial = useRef(load());
  const [conversations, setConversations] = useState<Conversation[]>(initial.current.list);
  const [activeId, setActiveId] = useState<string>(initial.current.activeId);

  useEffect(() => {
    try {
      localStorage.setItem(storageKeys.conversations, JSON.stringify(conversations));
      localStorage.setItem(storageKeys.activeConversation, activeId);
    } catch {
      // storage unavailable — history is in-memory only this session
    }
  }, [conversations, activeId]);

  const active = useMemo(
    () => conversations.find((c) => c.id === activeId) ?? conversations[0],
    [conversations, activeId],
  );

  const newConversation = useCallback(() => {
    // Reuse an existing empty conversation instead of piling up blanks.
    const blank = conversations.find((c) => c.messages.length === 0);
    if (blank) {
      setActiveId(blank.id);
      return;
    }
    const fresh = emptyConversation();
    setConversations([fresh, ...conversations].slice(0, MAX_CONVERSATIONS));
    setActiveId(fresh.id);
  }, [conversations]);

  const selectConversation = useCallback((id: string) => setActiveId(id), []);

  const deleteConversation = useCallback(
    (id: string) => {
      const next = conversations.filter((c) => c.id !== id);
      if (next.length === 0) {
        const fresh = emptyConversation();
        setConversations([fresh]);
        setActiveId(fresh.id);
        return;
      }
      setConversations(next);
      setActiveId((cur) => (cur === id ? next[0].id : cur));
    },
    [conversations],
  );

  const patchActive = useCallback(
    (
      patch:
        | Partial<Omit<Conversation, "id">>
        | ((current: Conversation) => Partial<Omit<Conversation, "id">>),
    ) => {
      setConversations((prev) =>
        prev.map((c) => {
          if (c.id !== activeId) return c;
          const delta = typeof patch === "function" ? patch(c) : patch;
          return { ...c, ...delta, updatedAt: Date.now() };
        }),
      );
    },
    [activeId],
  );

  const sorted = useMemo(
    () => [...conversations].sort((a, b) => b.updatedAt - a.updatedAt),
    [conversations],
  );

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
