# AEP-013: Security Hardening & Authentication Layer

| Field | Value |
|-------|-------|
| **Status** | <span class="badge badge--green">completed</span> |
| **Priority** | <span class="badge badge--red">P0</span> |
| **Effort** | High (7-10 days) |
| **Impact** | Critical |
| **Dependencies** | AEP-011 (deployment hardening) |

## Gap Analysis

### Current Implementation
The project has solid security foundations:
- **RBAC**: 3-role hierarchy (viewer/operator/admin) enforced via `GuardrailsPlugin`
- **Input validation**: 5 validators with safety constants preventing injection attacks
- **Guardrails**: `@destructive` and `@confirm` decorators for dangerous operations
- **Audit logging**: Structured JSON with secret redaction
- **Authentication enforcement**: `set_user_role()` marks server-trusted roles; `ensure_default_role()` forces `viewer` for unset roles

However, there is **no authentication layer** — the system trusts the integration layer
(Slack bot, web UI) to set the user role correctly.

### What ADK Provides
ADK's safety documentation recommends a multi-layered approach:

1. **Agent-Auth**: Service account identity for tool calls to external systems
2. **User-Auth**: OAuth-based identity delegation (agent acts as the user)
3. **In-tool guardrails**: Policy enforcement via `ToolContext` with developer-set constraints
4. **Gemini safety filters**: Configurable content safety thresholds
5. **Callbacks/Plugins for guardrails**: Pre-validation of model and tool I/O
6. **Gemini as a Judge**: LLM-based safety screening of inputs/outputs
7. **PII Redaction Plugin**: Before-tool callback to redact PII

### Gap
1. **No authentication**: Anyone with network access can send requests
2. **No OAuth/JWT verification**: User identity is self-declared
3. **No API key management**: LLM keys in environment variables
4. **No PII redaction**: Infrastructure data (IPs, hostnames) may leak
5. **No content safety filters**: No screening for prompt injection attempts
6. **No network isolation**: No guidance for VPC/firewall configuration

## Proposed Solution

### Step 1: JWT Authentication Middleware

```python
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer
import jwt

security = HTTPBearer()

async def verify_token(credentials = Depends(security)):
    try:
        payload = jwt.decode(
            credentials.credentials,
            os.getenv("JWT_SECRET"),
            algorithms=["HS256"],
        )
        return payload
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

# Map JWT claims to RBAC roles
def extract_role(token_payload: dict) -> str:
    roles = token_payload.get("roles", [])
    if "admin" in roles:
        return "admin"
    elif "operator" in roles:
        return "operator"
    return "viewer"
```

### Step 2: Auth Plugin

```python
class AuthPlugin(BasePlugin):
    """Validates authentication and sets user role from JWT claims."""

    def __init__(self):
        super().__init__(name="auth")

    async def on_user_message_callback(self, *, invocation_context, user_message):
        # Extract auth context from session state (set by HTTP middleware)
        auth_context = invocation_context.session.state.get("_auth")
        if not auth_context:
            return types.Content(
                parts=[types.Part(text="Authentication required.")],
                role="model",
            )

        # Set verified role
        set_user_role(invocation_context.session, auth_context["role"])
        return None
```

### Step 3: PII Redaction Plugin

Create a plugin that redacts infrastructure PII before tool output reaches the LLM:

```python
class PIIRedactionPlugin(BasePlugin):
    """Redacts infrastructure-sensitive data from tool outputs."""

    PATTERNS = [
        (r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', '[REDACTED_IP]'),
        (r'password["\s:=]+\S+', 'password=[REDACTED]'),
        (r'token["\s:=]+\S+', 'token=[REDACTED]'),
        (r'(?i)api[_-]?key["\s:=]+\S+', 'api_key=[REDACTED]'),
    ]

    def __init__(self):
        super().__init__(name="pii_redaction")

    async def after_tool_callback(self, *, tool, args, tool_context, result):
        if isinstance(result, dict):
            result = self._redact_dict(result)
        return result

    def _redact_dict(self, data):
        """Recursively redact sensitive patterns in dict values."""
        ...
```

### Step 4: Prompt Injection Detection

Add a safety screening plugin (inspired by ADK's "Gemini as a Judge" pattern):

```python
class SafetyScreenPlugin(BasePlugin):
    """Screens user inputs for prompt injection attempts."""

    INJECTION_PATTERNS = [
        r"ignore previous instructions",
        r"forget your instructions",
        r"you are now",
        r"system prompt",
        r"reveal your",
    ]

    async def on_user_message_callback(self, *, invocation_context, user_message):
        text = " ".join(p.text for p in user_message.parts if p.text)
        for pattern in self.INJECTION_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                logger.warning("Potential prompt injection detected", extra={"input": text[:200]})
                return types.Content(
                    parts=[types.Part(text="I can only help with DevOps tasks.")],
                    role="model",
                )
        return None
```

### Step 5: Gemini Safety Filters

For Gemini models, enable content safety filters:

```python
from google.genai import types

agent = create_agent(
    name="orrery_assistant",
    generate_content_config=types.GenerateContentConfig(
        safety_settings=[
            types.SafetySetting(
                category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
                threshold=types.HarmBlockThreshold.BLOCK_LOW_AND_ABOVE,
            ),
        ],
    ),
)
```

### Step 6: Secrets Management

Replace environment variables with a secrets manager:

```python
# core/orrery_core/secrets.py
class SecretsManager:
    """Pluggable secrets management."""

    @staticmethod
    def get(key: str) -> str:
        # Try vault first, fall back to env vars
        if vault_client := _get_vault_client():
            return vault_client.read(f"secret/data/agents/{key}")
        return os.getenv(key, "")
```

## Affected Files

| File | Change |
|------|--------|
| `core/orrery_core/auth.py` | New: JWT verification, AuthPlugin |
| `core/orrery_core/pii.py` | New: PIIRedactionPlugin |
| `core/orrery_core/safety.py` | New: SafetyScreenPlugin |
| `core/orrery_core/secrets.py` | New: SecretsManager |
| `core/orrery_core/plugins.py` | Add auth/PII/safety plugins to `default_plugins()` |
| `agents/slack-bot/slack_bot/app.py` | Add Slack signature verification |
| `core/tests/test_auth.py` | New: auth tests |
| `core/tests/test_pii.py` | New: PII redaction tests |
| `core/tests/test_safety.py` | New: prompt injection detection tests |

## Acceptance Criteria

- [x] JWT authentication required for HTTP endpoints
- [x] User role derived from verified JWT claims (not self-declared)
- [x] PII redaction applied to all tool outputs
- [x] Prompt injection patterns detected and blocked
- [x] Gemini safety filters enabled for content screening
- [x] Secrets manager with pluggable backends (mounted-file backend shipped;
      env var fallback; the `SecretsBackend` protocol accepts a Vault adapter
      without core changes)
- [x] Slack bot verifies request signatures (slack-bolt signing-secret
      verification; Socket Mode authenticates via app token)
- [x] All security features have comprehensive tests
- [x] Security documentation with threat model
      ([Security & Authentication](../config/security.md))

## Implementation notes (as landed)

The landed implementation diverges from the sketches above where ADK 2.0
semantics required it:

- **SafetyScreenPlugin blocks in `before_run_callback`, not
  `on_user_message_callback`** — in ADK 2.0 the latter can only *replace* the
  user message; `before_run_callback` is the hook whose non-None return halts
  the runner, so a screened message never reaches the model or any tool.
- **PIIRedactionPlugin mutates tool results in place and returns `None`** —
  ADK's after-tool chain early-exits on the first non-None return, so
  returning a redacted copy would silence every observer registered later
  (audit outcome, activity, metrics, output cap). It registers *before*
  `AuditPlugin` so the audit log records redacted values too. Key-based
  redaction (credential-named dict keys) is layered over value-pattern
  scanning (kv pairs, PEM blocks, AWS/GitHub/Slack/OpenAI token shapes,
  JWTs); IP redaction is opt-in (`ORRERY_REDACT_IPS`) because an SRE agent
  that cannot see pod/broker IPs cannot diagnose much.
- **Gemini safety filters default to `BLOCK_ONLY_HIGH`**, not
  `BLOCK_LOW_AND_ABOVE` — SRE conversations legitimately discuss killing
  pods and destroying volumes; the aggressive threshold trips on ops
  chatter. Threshold via `GEMINI_SAFETY_THRESHOLD`; only attached to Gemini
  string models (LiteLLM providers ignore the genai config).
- Both plugins are **on by default** in `default_plugins()`
  (`ORRERY_SAFETY_SCREEN=false` / `ORRERY_PII_REDACTION=false` to disable).

## Notes

- Authentication is the most critical gap. Without it, RBAC is meaningless since anyone can claim to be an admin.
- Start with JWT + RBAC mapping. OAuth2 with a proper identity provider (Auth0, Keycloak, Google IAP) is the production target.
- The PII redaction plugin should be configurable — some users may need to see IPs and hostnames for debugging.
- Prompt injection detection via regex is a baseline. For production, consider the "Gemini as a Judge" pattern using a fast, cheap model (Gemini Flash Lite) to screen inputs.
