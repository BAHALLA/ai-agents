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
