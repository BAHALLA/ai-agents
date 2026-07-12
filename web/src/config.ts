/**
 * Runtime configuration, resolved once at module load.
 *
 * The console is served same-origin by the FastAPI front door in production,
 * so the API base defaults to "" (relative URLs hit the serving host). A full
 * URL is only needed for cross-origin dev.
 */
export const config = {
  /** Base URL for API calls; "" means same-origin. Never has a trailing slash. */
  apiBaseUrl: (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/+$/, ""),
} as const;

/** localStorage keys — namespaced to avoid collisions with other apps on the host. */
export const storageKeys = {
  token: "orrery.console.token",
  sessionId: "orrery.console.sessionId",
} as const;
