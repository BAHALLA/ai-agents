import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiClient, ApiError } from "../api/client";
import type { ActivityEntry, PendingConfirmation, TriageResponse } from "../api/types";
import type { ConversationsController } from "../conversations/useConversations";
import { NEW_CONVERSATION_TITLE } from "../conversations/types";
import type { ChatError, ChatMessage } from "./types";

let idCounter = 0;
function nextId(): string {
  idCounter += 1;
  return `${Date.now()}-${idCounter}`;
}

/** The canned prompt behind the "Run triage" button — routes to the
 * incident_triage_agent AgentTool on the chat root. */
export const TRIAGE_PROMPT =
  "Run a full incident triage across all systems and report the verdict.";

/** First line of the first user message, trimmed for the sidebar. */
function titleFrom(text: string): string {
  const line = text.split("\n")[0].trim();
  return line.length > 48 ? `${line.slice(0, 47)}…` : line || NEW_CONVERSATION_TITLE;
}

export interface ChatController {
  messages: ChatMessage[];
  sessionId: string | null;
  isSending: boolean;
  error: ChatError | null;
  /** Tool calls recorded for this session (the inspector table). */
  activity: ActivityEntry[];
  /** The caller's guarded action awaiting approve/deny, if any. */
  pending: PendingConfirmation | null;
  /** The session's latest triage verdict, if one was recorded. */
  triage: TriageResponse | null;
  send: (text: string) => Promise<void>;
  /** Send an approve/deny decision through the normal chat flow. */
  decide: (word: "approve" | "deny") => Promise<void>;
  /** Kick off a full triage sweep (the canned prompt). */
  runTriage: () => Promise<void>;
}

/**
 * Drives one conversation against the /chat endpoint.
 *
 * The transcript and the server session id live in the active conversation
 * (owned by {@link useConversations}); this hook owns the request lifecycle
 * and the transient inspector state (activity / pending / triage), resetting
 * and reloading them whenever the active conversation changes.
 */
export function useChat(token: string | null, conv: ConversationsController): ChatController {
  const client = useMemo(() => new ApiClient(token), []); // eslint-disable-line react-hooks/exhaustive-deps
  const [isSending, setIsSending] = useState(false);
  const [error, setError] = useState<ChatError | null>(null);
  const [activity, setActivity] = useState<ActivityEntry[]>([]);
  const [pending, setPending] = useState<PendingConfirmation | null>(null);
  const [triage, setTriage] = useState<TriageResponse | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const { active, activeId, patchActive } = conv;
  const messages = active.messages;
  const sessionId = active.sessionId;

  // Keep the client's token current without recreating it (preserves identity).
  useEffect(() => {
    client.setToken(token);
  }, [client, token]);

  useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  // Best-effort inspector panes: the timeline, pending-confirmation, and
  // triage state are rendering data, so a failed refresh never surfaces as a
  // chat error.
  const refreshSidePanes = useCallback(
    async (id: string) => {
      const [act, pend, tri] = await Promise.allSettled([
        client.activity(id),
        client.pendingConfirmation(),
        client.triage(id),
      ]);
      if (act.status === "fulfilled") setActivity(act.value.entries ?? []);
      if (pend.status === "fulfilled") setPending(pend.value.pending ?? null);
      if (tri.status === "fulfilled") setTriage(tri.value.severity ? tri.value : null);
    },
    [client],
  );

  // Switching conversations: drop the previous inspector state, then reload
  // this conversation's timeline/triage from the server if it has a session.
  useEffect(() => {
    abortRef.current?.abort();
    setIsSending(false);
    setError(null);
    setActivity([]);
    setPending(null);
    setTriage(null);
    if (sessionId) void refreshSidePanes(sessionId);
    // Only re-run when the active conversation changes, not on every message.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId]);

  // While a request is in flight, poll the timeline so multi-specialist
  // sweeps become visible as each specialist completes — the closest thing
  // to progress until streaming (AEP-009) lands.
  useEffect(() => {
    if (!isSending || !sessionId) return;
    const timer = setInterval(() => void refreshSidePanes(sessionId), 2500);
    return () => clearInterval(timer);
  }, [isSending, sessionId, refreshSidePanes]);

  const send = useCallback(
    async (raw: string) => {
      const text = raw.trim();
      if (!text || isSending) return;

      setError(null);
      const userMessage: ChatMessage = { id: nextId(), role: "user", text, at: Date.now() };
      patchActive((c) => ({
        messages: [...c.messages, userMessage],
        title: c.title === NEW_CONVERSATION_TITLE ? titleFrom(text) : c.title,
      }));
      setIsSending(true);

      const controller = new AbortController();
      abortRef.current = controller;

      try {
        const res = await client.chat(
          { message: text, session_id: sessionId },
          {
            signal: controller.signal,
          },
        );
        patchActive((c) => ({
          sessionId: res.session_id,
          messages: [
            ...c.messages,
            { id: nextId(), role: "assistant", text: res.response, at: Date.now() },
          ],
        }));
        await refreshSidePanes(res.session_id);
      } catch (err) {
        if (err instanceof DOMException && err.name === "AbortError") return;
        if (err instanceof ApiError) {
          setError({ message: err.message, isAuth: err.isAuth });
        } else {
          setError({ message: "Unexpected error sending message.", isAuth: false });
        }
      } finally {
        setIsSending(false);
        abortRef.current = null;
      }
    },
    [client, sessionId, isSending, patchActive, refreshSidePanes],
  );

  // The decision is a plain chat message: the requester-verified gate on the
  // server decides whether it authorizes anything. The panel is a renderer.
  const decide = useCallback((word: "approve" | "deny") => send(word), [send]);

  const runTriage = useCallback(() => send(TRIAGE_PROMPT), [send]);

  return {
    messages,
    sessionId,
    isSending,
    error,
    activity,
    pending,
    triage,
    send,
    decide,
    runTriage,
  };
}
