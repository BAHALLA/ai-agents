import { useEffect, useRef } from "react";
import type { ChatMessage } from "../chat/types";

interface Props {
  messages: ChatMessage[];
  isSending: boolean;
}

function formatTime(at: number): string {
  return new Date(at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/** Scrolling transcript. Auto-scrolls to the newest message on change. */
export function MessageList({ messages, isSending }: Props) {
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, isSending]);

  if (messages.length === 0 && !isSending) {
    return (
      <div className="transcript transcript--empty">
        <p>Ask about the health of your Kafka, Kubernetes, Elasticsearch, or Docker estate.</p>
        <p className="transcript__examples">
          e.g. <em>“What's the health of my Kafka cluster?”</em>
        </p>
      </div>
    );
  }

  return (
    <div className="transcript" aria-live="polite">
      {messages.map((m) => (
        <div key={m.id} className={`bubble bubble--${m.role}`}>
          <div className="bubble__meta">
            <span className="bubble__author">{m.role === "user" ? "You" : "Orrery"}</span>
            <time className="bubble__time" dateTime={new Date(m.at).toISOString()}>
              {formatTime(m.at)}
            </time>
          </div>
          <div className="bubble__text">{m.text}</div>
        </div>
      ))}
      {isSending ? (
        <div className="bubble bubble--assistant bubble--pending" aria-label="Orrery is thinking">
          <span className="dots">
            <span />
            <span />
            <span />
          </span>
        </div>
      ) : null}
      <div ref={endRef} />
    </div>
  );
}
