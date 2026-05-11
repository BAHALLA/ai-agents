# scripts/

Local development and testing scripts. **Not** part of the published
package or the production image — these are hand-tools, not contracts.

## What's here

| Script | Purpose |
|--------|---------|
| `dev_token.py` | Mint a short-lived HS256 JWT for hitting the local server. Picks role from a flag. |
| `run_auth_demo.sh` | Boot `orrery_assistant` with auth on, then walk through 401 / 200 paths against `/chat`. |

## Conventions

- Keep scripts **idempotent** — running twice should produce the same outcome (no leftover processes, no half-written state).
- Use `set -euo pipefail` at the top of every bash script.
- Trap `EXIT` for cleanup (kill background servers, remove temp files).
- Default to **dev-safe values**: in-memory sessions, short token expiry, localhost binds. Never bake real secrets into scripts here.
- Name personal / one-off scripts `*.local.sh` or `*.local.py` — `.gitignore` excludes them so you can keep private helpers in this directory without committing them.

## Quick start

### Two-terminal flow (long-lived dev server)

Terminal 1 — boot the server with auth on. First run generates a persistent
secret at `~/.cache/orrery/jwt-secret`:

```bash
make run-assistant-api
```

Terminal 2 — mint a token against the same secret and call `/chat`:

```bash
TOKEN=$(make dev-token-admin)        # or dev-token-viewer / dev-token-operator
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"hi"}'
```

### One-shot end-to-end smoke test (no terminal juggling)

```bash
./scripts/run_auth_demo.sh             # boots server, runs 6 auth + LLM checks, tears down
./scripts/run_auth_demo.sh --skip-chat # 4 checks, no LLM credits needed
./scripts/run_auth_demo.sh --verbose   # full request/response bodies
```

### Token minting details

The `dev_token.py` script resolves the signing secret in this order, so it
always matches whatever the server is using:

1. `--secret <value>` (CLI arg)
2. `--secret-file <path>` (CLI arg)
3. `$JWT_SECRET` environment variable
4. `~/.cache/orrery/jwt-secret` (the file `make run-assistant-api` writes)
5. Built-in dev fallback (prints a warning — tokens won't validate against
   a server using a different secret)

Useful flags: `--role`, `--subject`, `--audience`, `--issuer`, `--expires-in`
(supports negative for expired-token testing), `--decode` (echoes claims to
stderr).

### Make targets

| Target | What it does |
|--------|--------------|
| `make run-assistant-api`       | Boot the FastAPI server with auth ON, port 8000. |
| `make dev-token ROLE=admin`    | Mint a JWT with the given role. |
| `make dev-token-viewer`        | Convenience alias. |
| `make dev-token-operator`      | Convenience alias. |
| `make dev-token-admin`         | Convenience alias. |
| `make print-dev-jwt-config`    | Show the dev secret path + audience + issuer. |
| `make rotate-dev-jwt-secret`   | Regenerate the secret (invalidates outstanding tokens). |
