/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string;
  /** OIDC issuer URL. Unset disables SSO and falls back to the token gate. */
  readonly VITE_OIDC_ISSUER?: string;
  readonly VITE_OIDC_CLIENT_ID?: string;
  readonly VITE_OIDC_SCOPE?: string;
  /** Dotted path to the roles claim; must match the server's JWT_ROLE_CLAIM. */
  readonly VITE_OIDC_ROLE_CLAIM?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
