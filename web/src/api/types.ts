/**
 * Wire types for the Orrery HTTP front door.
 *
 * These mirror `core/orrery_core/serving/server.py` (ChatRequest / ChatResponse).
 * Until AEP-019's typed-boundary work generates these from the FastAPI OpenAPI
 * schema (`openapi-typescript`), they are hand-maintained — keep them in sync
 * with the server models.
 */

/** POST /chat request body. */
export interface ChatRequest {
  message: string;
  /** Omit / null on the first turn; the server mints and returns a session id. */
  session_id?: string | null;
}

/** POST /chat success response. */
export interface ChatResponse {
  session_id: string;
  response: string;
}

/** The three roles the platform's RBAC resolves to. */
export type Role = "viewer" | "operator" | "admin";

/** One recorded tool call (shape written by ActivityPlugin server-side). */
export interface ActivityEntry {
  operation: string;
  details: string;
  timestamp: string;
}

/** GET /session/{id}/activity response. */
export interface ActivityResponse {
  session_id: string;
  entries: ActivityEntry[];
}

/** The caller's guarded action awaiting an approve/deny decision. */
export interface PendingConfirmation {
  tool_name: string;
  level: string;
  args: Record<string, unknown>;
  created_at: number;
}

/** GET /confirmations/pending response. */
export interface PendingResponse {
  pending: PendingConfirmation | null;
}
