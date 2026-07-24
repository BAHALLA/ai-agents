import { useEffect, useRef } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ChatMessage } from "../chat/types";

interface Props {
  messages: ChatMessage[];
  isSending: boolean;
}

function formatTime(at: number): string {
  return new Date(at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/** How far from the bottom still counts as "following along" (px). */
const STICK_THRESHOLD = 80;

/**
 * Scrolling transcript. Follows the newest message *only while the reader is
 * already at the bottom* — scrolling up to re-read an earlier answer while a
 * multi-specialist sweep is still landing must not yank them back down.
 */
export function MessageList({ messages, isSending }: Props) {
  const endRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);

  const handleScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    stickToBottom.current = distance <= STICK_THRESHOLD;
  };

  useEffect(() => {
    if (!stickToBottom.current) return;
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, isSending]);

  if (messages.length === 0 && !isSending) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-2 p-8 text-center text-slate-500 dark:text-slate-400">
        <p className="max-w-md">
          Ask about the health of your Kafka, Kubernetes, Elasticsearch, or Docker estate.
        </p>
        <p className="text-sm">
          e.g. <em>“What's the health of my Kafka cluster?”</em>
        </p>
      </div>
    );
  }

  return (
    <div
      ref={scrollRef}
      onScroll={handleScroll}
      className="orrery-scroll flex flex-1 flex-col gap-4 overflow-y-auto p-4 sm:p-6"
      role="log"
      aria-label="Conversation transcript"
      aria-live="polite"
    >
      {messages.map((m) => {
        const isUser = m.role === "user";
        return (
          <div
            key={m.id}
            className={`flex max-w-2xl flex-col gap-1 ${isUser ? "self-end items-end" : "self-start items-start"}`}
          >
            <div className="flex items-center gap-2 px-1 text-xs text-slate-400 dark:text-slate-500">
              <span className="font-medium text-slate-500 dark:text-slate-400">
                {isUser ? "You" : "Orrery"}
              </span>
              <time dateTime={new Date(m.at).toISOString()}>{formatTime(m.at)}</time>
            </div>
            <div
              className={
                isUser
                  ? "rounded-2xl rounded-tr-sm bg-blue-600 px-4 py-2.5 text-white"
                  : "rounded-2xl rounded-tl-sm bg-slate-100 px-4 py-2.5 text-slate-800 dark:bg-slate-800 dark:text-slate-100"
              }
            >
              {isUser ? (
                <p className="whitespace-pre-wrap">{m.text}</p>
              ) : (
                // Agents reply in markdown; react-markdown builds React elements
                // (no dangerouslySetInnerHTML), so no sanitizer is needed.
                <div className="prose prose-sm dark:prose-invert max-w-none prose-pre:bg-slate-900 prose-pre:text-slate-100">
                  <Markdown remarkPlugins={[remarkGfm]}>{m.text}</Markdown>
                </div>
              )}
            </div>
          </div>
        );
      })}
      {isSending ? (
        <div
          className="flex items-center gap-1.5 self-start rounded-2xl rounded-tl-sm bg-slate-100 px-4 py-3 dark:bg-slate-800"
          aria-label="Orrery is thinking"
        >
          <span className="orrery-dot h-2 w-2 rounded-full bg-slate-400" />
          <span className="orrery-dot h-2 w-2 rounded-full bg-slate-400" />
          <span className="orrery-dot h-2 w-2 rounded-full bg-slate-400" />
        </div>
      ) : null}
      <div ref={endRef} />
    </div>
  );
}
