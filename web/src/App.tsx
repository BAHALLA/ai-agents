import { useAuth } from "./auth/useAuth";
import { ChatConsole } from "./components/ChatConsole";
import { TokenGate } from "./components/TokenGate";

export function App() {
  const auth = useAuth();

  if (!auth.isAuthenticated || auth.token === null) {
    const expiredMsg = auth.token !== null ? "That token has expired. Paste a fresh one." : null;
    return <TokenGate onSubmit={auth.signIn} error={expiredMsg} />;
  }

  return <ChatConsole token={auth.token} identity={auth.identity} onSignOut={auth.signOut} />;
}
