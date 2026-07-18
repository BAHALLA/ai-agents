import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiClient, ApiError } from "../api/client";
import type { ActivityEntry, PendingConfirmation, TriageResponse } from "../api/types";
import { storageKeys } from "../config";
import type { ChatError, ChatMessage } from "./types";

let idCounter = 0;
function nextId(): string {
  idCounter += 1;
  return `${Date.now()}-${idCounter}`;
}

function readStoredSession(): string | null {
  try {
    return localStorage.getItem(storageKeys.sessionId);
  } catch {
    return null;
  }
}

/** The canned prompt behind the "Run triage" button — routes to the
 * incident_triage_agent AgentTool on the chat root. */
export const TRIAGE_PROMPT =
  "Run a full incident triage across all systems and report the verdict.";

export interface ChatController {
  messages: ChatMessage[];
  sessionId: string | null;
  isSending: boolean;
  error: ChatError | null;
  /** Tool calls recorded for this session (the timeline pane). */
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
  reset: () => void;
}

/**
 * Drives one conversation against the /chat endpoint.
 *
 * Keeps the transcript in memory and threads the server-issued `session_id`
 * across turns (persisted so a reload resumes the same session). A single
 * in-flight request is enforced and aborted on unmount to avoid setState on an
 * unmounted component.
 */
export function useChat(token: string | null): ChatController {
  const client = useMemo(() => new ApiClient(token), []); // eslint-disable-line react-hooks/exhaustive-deps
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(readStoredSession);
  const [isSending, setIsSending] = useState(false);
  const [error, setError] = useState<ChatError | null>(null);
  const [activity, setActivity] = useState<ActivityEntry[]>([]);
  const [pending, setPending] = useState<PendingConfirmation | null>(null);
  const [triage, setTriage] = useState<TriageResponse | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  // Keep the client's token current without recreating it (preserves identity).
  useEffect(() => {
    client.setToken(token);
  }, [client, token]);

  useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  const persistSession = useCallback((id: string) => {
    setSessionId(id);
    try {
      localStorage.setItem(storageKeys.sessionId, id);
    } catch {
      // ignore
    }
  }, []);

  // Best-effort side panes: the timeline, pending-confirmation, and triage
  // state are rendering data, so a failed refresh never surfaces as a chat
  // error.
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
      setMessages((prev) => [...prev, userMessage]);
      setIsSending(true);

      const controller = new AbortController();
      abortRef.current = controller;

      try {
        const res = await client.chat(
          { message: text, session_id: sessionId },
          { signal: controller.signal },
        );
        persistSession(res.session_id);
        setMessages((prev) => [
          ...prev,
          { id: nextId(), role: "assistant", text: res.response, at: Date.now() },
        ]);
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
    [client, sessionId, isSending, persistSession, refreshSidePanes],
  );

  // The decision is a plain chat message: the requester-verified gate on the
  // server decides whether it authorizes anything. The panel is a renderer.
  const decide = useCallback((word: "approve" | "deny") => send(word), [send]);

  const runTriage = useCallback(() => send(TRIAGE_PROMPT), [send]);

  const reset = useCallback(() => {
    abortRef.current?.abort();
    setMessages([]);
    setError(null);
    setSessionId(null);
    setActivity([]);
    setPending(null);
    setTriage(null);
    try {
      localStorage.removeItem(storageKeys.sessionId);
    } catch {
      // ignore
    }
  }, []);

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
    reset,
  };
}
