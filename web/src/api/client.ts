import { config } from "../config";
import type {
  ActivityResponse,
  ChatRequest,
  ChatResponse,
  MeResponse,
  PendingResponse,
  SelfTestResponse,
  TriageResponse,
} from "./types";

/** A typed API error carrying the HTTP status so callers can branch on 401 etc. */
export class ApiError extends Error {
  readonly status: number;
  /** Seconds to wait before retrying, from a 429's Retry-After header. */
  readonly retryAfter: number | null;

  constructor(status: number, message: string, retryAfter: number | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.retryAfter = retryAfter;
  }

  /** True when the failure is an auth problem the user can fix by re-entering a token. */
  get isAuth(): boolean {
    return this.status === 401 || this.status === 403;
  }

  /** True when the caller was rate limited and should simply wait. */
  get isRateLimited(): boolean {
    return this.status === 429;
  }

  /** True when retrying the identical request could plausibly succeed. */
  get isRetryable(): boolean {
    return this.status === 0 || this.status === 429 || this.status >= 500;
  }
}

interface RequestOptions {
  signal?: AbortSignal;
}

/**
 * Minimal, dependency-free API client for the front door.
 *
 * Holds the bearer token in memory (the caller owns persistence) and attaches
 * it to every request. Kept deliberately small: one method per endpoint, typed
 * in and out, with structured errors.
 */
export class ApiClient {
  private token: string | null;

  constructor(token: string | null = null) {
    this.token = token;
  }

  setToken(token: string | null): void {
    this.token = token;
  }

  /** `body === undefined` means a GET request; anything else is POSTed as JSON. */
  private async request<T>(path: string, body: unknown, options: RequestOptions = {}): Promise<T> {
    const headers: Record<string, string> = {};
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (this.token) headers.Authorization = `Bearer ${this.token}`;

    let res: Response;
    try {
      res = await fetch(`${config.apiBaseUrl}${path}`, {
        method: body !== undefined ? "POST" : "GET",
        headers,
        body: body !== undefined ? JSON.stringify(body) : null,
        signal: options.signal ?? null,
      });
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") throw err;
      // Network-level failure (server down, DNS, CORS). 0 = "no HTTP response".
      throw new ApiError(0, "Network error — is the server reachable?");
    }

    if (!res.ok) {
      throw new ApiError(res.status, await extractError(res), retryAfterSeconds(res));
    }
    return (await res.json()) as T;
  }

  /** Send one chat turn. Pass the returned session_id back on the next call. */
  chat(req: ChatRequest, options?: RequestOptions): Promise<ChatResponse> {
    return this.request<ChatResponse>("/chat", req, options);
  }

  /** Tool-call timeline for one of the caller's sessions. */
  activity(sessionId: string, options?: RequestOptions): Promise<ActivityResponse> {
    return this.request<ActivityResponse>(
      `/session/${encodeURIComponent(sessionId)}/activity`,
      undefined,
      options,
    );
  }

  /** The caller's own guarded action awaiting approve/deny, if any. */
  pendingConfirmation(options?: RequestOptions): Promise<PendingResponse> {
    return this.request<PendingResponse>("/confirmations/pending", undefined, options);
  }

  /** The session's latest triage verdict (severity + report), if any. */
  triage(sessionId: string, options?: RequestOptions): Promise<TriageResponse> {
    return this.request<TriageResponse>(
      `/session/${encodeURIComponent(sessionId)}/triage`,
      undefined,
      options,
    );
  }

  /** The server's own view of the caller: resolved role, autonomy level, model. */
  me(options?: RequestOptions): Promise<MeResponse> {
    return this.request<MeResponse>("/me", undefined, options);
  }

  /** Run the first-run environment check (model + each wired integration). */
  selfTest(options?: RequestOptions): Promise<SelfTestResponse> {
    return this.request<SelfTestResponse>("/onboarding/selftest", {}, options);
  }
}

/** Parse `Retry-After` (delta-seconds or an HTTP date) into whole seconds. */
function retryAfterSeconds(res: Response): number | null {
  const header = res.headers.get("Retry-After");
  if (!header) return null;
  const seconds = Number(header);
  if (Number.isFinite(seconds)) return Math.max(0, Math.round(seconds));
  const when = Date.parse(header);
  return Number.isNaN(when) ? null : Math.max(0, Math.round((when - Date.now()) / 1000));
}

/** Pull a human-readable message out of a non-2xx response, tolerating any shape. */
async function extractError(res: Response): Promise<string> {
  try {
    const data: unknown = await res.json();
    if (data && typeof data === "object" && "detail" in data) {
      const { detail } = data;
      if (typeof detail === "string") return detail;
    }
  } catch {
    // fall through to status text
  }
  return res.statusText || `Request failed (${res.status})`;
}
