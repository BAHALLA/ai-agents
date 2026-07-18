import { useEffect, useRef, useState } from "react";
import type { Identity } from "../auth/token";
import { useChat } from "../chat/useChat";
import { useConversations } from "../conversations/useConversations";
import { ConfirmationPanel } from "./ConfirmationPanel";
import { InspectorPanel, type InspectorTab } from "./InspectorPanel";
import { MessageInput } from "./MessageInput";
import { MessageList } from "./MessageList";
import { severityBadge } from "./severity";
import { Sidebar } from "./Sidebar";

interface Props {
  token: string;
  identity: Identity | null;
  onSignOut: () => void;
}

/** The authenticated console: sidebar + chat column + inspector panel. */
export function ChatConsole({ token, identity, onSignOut }: Props) {
  const conversations = useConversations();
  const chat = useChat(token, conversations);

  const [inspectorTab, setInspectorTab] = useState<InspectorTab>("tools");
  const [inspectorOpen, setInspectorOpen] = useState(false);

  // Surface a fresh triage verdict without the user hunting for it.
  const lastSeverity = useRef<string | null>(null);
  useEffect(() => {
    const sev = chat.triage?.severity ?? null;
    if (sev && sev !== lastSeverity.current) {
      setInspectorTab("triage");
      setInspectorOpen(true);
    }
    lastSeverity.current = sev;
  }, [chat.triage]);

  const toggleInspector = (tab: InspectorTab) => {
    if (inspectorOpen && inspectorTab === tab) {
      setInspectorOpen(false);
    } else {
      setInspectorTab(tab);
      setInspectorOpen(true);
    }
  };

  const headerBtn = (active: boolean) =>
    `rounded-lg px-2.5 py-1.5 text-sm font-medium ${
      active
        ? "bg-slate-200 text-slate-800 dark:bg-slate-700 dark:text-slate-100"
        : "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800"
    }`;

  return (
    <div className="flex h-screen bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
      <Sidebar
        conversations={conversations}
        identity={identity}
        isSending={chat.isSending}
        onNewChat={conversations.newConversation}
        onRunTriage={() => void chat.runTriage()}
        onSignOut={onSignOut}
      />

      <main className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center gap-3 border-b border-slate-200 bg-white px-4 py-3 dark:border-slate-800 dark:bg-slate-900">
          <h2 className="min-w-0 flex-1 truncate font-medium text-slate-800 dark:text-slate-100">
            {conversations.active.title}
          </h2>
          <button
            type="button"
            className={headerBtn(inspectorOpen && inspectorTab === "tools")}
            onClick={() => toggleInspector("tools")}
          >
            Tool calls
            {chat.activity.length > 0 ? (
              <span className="ml-1.5 rounded-full bg-slate-200 px-1.5 text-xs tabular-nums text-slate-600 dark:bg-slate-700 dark:text-slate-300">
                {chat.activity.length}
              </span>
            ) : null}
          </button>
          <button
            type="button"
            className={headerBtn(inspectorOpen && inspectorTab === "triage")}
            onClick={() => toggleInspector("triage")}
          >
            {chat.triage?.severity ? (
              <span className="inline-flex items-center gap-1.5">
                Triage
                <span
                  className={`rounded-full px-1.5 py-0.5 text-xs font-semibold uppercase ${severityBadge[chat.triage.severity]}`}
                >
                  {chat.triage.severity}
                </span>
              </span>
            ) : (
              "Triage"
            )}
          </button>
        </header>

        <div className="flex min-h-0 flex-1">
          <section className="flex min-w-0 flex-1 flex-col">
            <MessageList messages={chat.messages} isSending={chat.isSending} />

            {chat.pending ? (
              <ConfirmationPanel
                pending={chat.pending}
                disabled={chat.isSending}
                onDecide={(word) => void chat.decide(word)}
              />
            ) : null}

            {chat.error ? (
              <div
                role="alert"
                className="mx-4 mb-2 flex items-center justify-between gap-3 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950/50 dark:text-red-300"
              >
                <span>{chat.error.message}</span>
                {chat.error.isAuth ? (
                  <button
                    type="button"
                    className="shrink-0 font-medium underline"
                    onClick={onSignOut}
                  >
                    Re-enter token
                  </button>
                ) : null}
              </div>
            ) : null}

            <MessageInput disabled={chat.isSending} onSend={(text) => void chat.send(text)} />
          </section>

          {inspectorOpen ? (
            <InspectorPanel
              tab={inspectorTab}
              onTab={setInspectorTab}
              onClose={() => setInspectorOpen(false)}
              activity={chat.activity}
              triage={chat.triage}
            />
          ) : null}
        </div>
      </main>
    </div>
  );
}
