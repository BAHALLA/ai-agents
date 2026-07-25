import { useAuth } from "./auth/useAuth";
import { ChatConsole } from "./components/ChatConsole";
import { SignInPanel } from "./components/SignInPanel";
import { TokenGate } from "./components/TokenGate";

export function App() {
  const auth = useAuth();

  if (!auth.isAuthenticated || auth.token === null) {
    // SSO deployments never show the paste-a-token gate; token deployments
    // never show the SSO button. The mode is deployment configuration, so
    // offering both would only invite the wrong one to be tried.
    if (auth.mode === "oidc") {
      return (
        <SignInPanel onSignIn={auth.signInWithSso} error={auth.error} isLoading={auth.isLoading} />
      );
    }
    const expiredMsg = auth.token !== null ? "That token has expired. Paste a fresh one." : null;
    return <TokenGate onSubmit={auth.signIn} error={auth.error ?? expiredMsg} />;
  }

  return <ChatConsole token={auth.token} identity={auth.identity} onSignOut={auth.signOut} />;
}
