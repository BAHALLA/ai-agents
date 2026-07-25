import { type User, UserManager, WebStorageStateStore } from "oidc-client-ts";
import { isOidcEnabled, oidcConfig } from "../config";

/**
 * OIDC single sign-on for the console (Authorization Code + PKCE).
 *
 * Provider-agnostic — the same configuration works against Keycloak, Authentik,
 * Auth0, Okta, Entra ID, or Google. PKCE with a public client is the correct
 * pattern for a browser SPA: there is nowhere to keep a client secret, and the
 * code challenge is what stops an intercepted authorization code being redeemed
 * by anyone else.
 *
 * Two deliberate choices:
 *
 * - **`redirect_uri` is the console root**, not a sub-path. The FastAPI front
 *   door serves the bundle with `StaticFiles(html=True)` (server.py), which
 *   404s unknown deep paths — a `/auth/callback` route would work under the
 *   Vite dev server and break in production. Returning to `/` with the code and
 *   state as query params needs no SPA router and no server change.
 * - **The access token lives in memory**, held by the UserManager and refreshed
 *   silently, rather than in localStorage as the paste-a-token flow does. Only
 *   oidc-client-ts's own session record is persisted, so a stolen localStorage
 *   snapshot doesn't hand over a bearer token.
 */

/** Where the provider sends the browser back to. Always the console root. */
export function redirectUri(): string {
  return `${window.location.origin}/`;
}

let manager: UserManager | null = null;

/** The shared UserManager, or null when SSO isn't configured. */
export function getUserManager(): UserManager | null {
  if (!isOidcEnabled) return null;
  if (manager) return manager;

  manager = new UserManager({
    authority: oidcConfig.issuer,
    client_id: oidcConfig.clientId,
    redirect_uri: redirectUri(),
    post_logout_redirect_uri: redirectUri(),
    response_type: "code",
    scope: oidcConfig.scope,
    // Renew in the background so a long incident session doesn't hit a login
    // wall mid-investigation.
    automaticSilentRenew: true,
    // Session records only — never the access token itself.
    userStore: new WebStorageStateStore({ store: window.sessionStorage }),
    stateStore: new WebStorageStateStore({ store: window.sessionStorage }),
  });
  return manager;
}

/** True when the current URL carries an authorization response. */
export function hasAuthResponse(search: string = window.location.search): boolean {
  const params = new URLSearchParams(search);
  return params.has("code") || params.has("error");
}

/**
 * Complete a redirect sign-in and strip the code/state from the address bar.
 *
 * The parameters are removed with `replaceState` so a refresh doesn't attempt
 * to redeem an already-used authorization code (which the provider rejects) and
 * so the code never lingers in browser history.
 */
export async function completeSignIn(): Promise<User | null> {
  const um = getUserManager();
  if (!um) return null;
  try {
    return await um.signinRedirectCallback();
  } finally {
    window.history.replaceState({}, document.title, window.location.pathname);
  }
}

/** Restore an existing session without contacting the provider. */
export async function loadUser(): Promise<User | null> {
  const um = getUserManager();
  if (!um) return null;
  const user = await um.getUser();
  return user && !user.expired ? user : null;
}

export async function signInRedirect(): Promise<void> {
  await getUserManager()?.signinRedirect();
}

/** Sign out of the provider too — a local-only sign-out silently signs back in. */
export async function signOutRedirect(): Promise<void> {
  const um = getUserManager();
  if (!um) return;
  try {
    await um.signoutRedirect();
  } catch {
    // Providers without an end-session endpoint (or with it disabled) reject
    // this. Dropping the local session is still the right outcome.
    await um.removeUser();
  }
}
