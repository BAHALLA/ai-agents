import { useChat } from "../chat/useChat";
import type { Identity } from "../auth/token";
import { ConfirmationPanel } from "./ConfirmationPanel";
import { IdentityBadge } from "./IdentityBadge";
import { MessageInput } from "./MessageInput";
import { MessageList } from "./MessageList";
import { ToolTimeline } from "./ToolTimeline";

interface Props {
  token: string;
  identity: Identity | null;
  onSignOut: () => void;
}

/** The authenticated console: header + transcript + composer. */
export function ChatConsole({ token, identity, onSignOut }: Props) {
  const chat = useChat(token);

  const handleSignOut = () => {
    chat.reset();
    onSignOut();
  };

  return (
    <div className="console">
      <header className="console__header">
        <div className="console__brand">
          <span className="console__logo" aria-hidden="true">
            🛰️
          </span>
          <span className="console__title">Orrery Console</span>
        </div>
        <div className="console__header-right">
          {chat.messages.length > 0 ? (
            <button type="button" className="btn btn--ghost" onClick={chat.reset}>
              New chat
            </button>
          ) : null}
          <IdentityBadge identity={identity} onSignOut={handleSignOut} />
        </div>
      </header>

      <main className="console__body">
        <MessageList messages={chat.messages} isSending={chat.isSending} />
        <ToolTimeline entries={chat.activity} />
      </main>

      {chat.pending ? (
        <ConfirmationPanel
          pending={chat.pending}
          disabled={chat.isSending}
          onDecide={(word) => void chat.decide(word)}
        />
      ) : null}

      {chat.error ? (
        <div
          className={`banner banner--error${chat.error.isAuth ? " banner--auth" : ""}`}
          role="alert"
        >
          {chat.error.message}
          {chat.error.isAuth ? (
            <button type="button" className="btn btn--ghost" onClick={handleSignOut}>
              Re-enter token
            </button>
          ) : null}
        </div>
      ) : null}

      <footer className="console__footer">
        <MessageInput disabled={chat.isSending} onSend={(text) => void chat.send(text)} />
      </footer>
    </div>
  );
}
