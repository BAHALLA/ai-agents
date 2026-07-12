import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiClient, ApiError } from "../api/client";
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

export interface ChatController {
  messages: ChatMessage[];
  sessionId: string | null;
  isSending: boolean;
  error: ChatError | null;
  send: (text: string) => Promise<void>;
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
    [client, sessionId, isSending, persistSession],
  );

  const reset = useCallback(() => {
    abortRef.current?.abort();
    setMessages([]);
    setError(null);
    setSessionId(null);
    try {
      localStorage.removeItem(storageKeys.sessionId);
    } catch {
      // ignore
    }
  }, []);

  return { messages, sessionId, isSending, error, send, reset };
}
