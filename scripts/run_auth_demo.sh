#!/usr/bin/env bash
# End-to-end smoke test of the authenticated HTTP front door.
#
# Boots `orrery_assistant` on a free port with AUTH_ENABLED=true and a
# generated JWT_SECRET, then walks through every interesting status code
# against POST /chat:
#
#   1. /healthz                              → 200  (unauthenticated by design)
#   2. /chat without bearer token            → 401
#   3. /chat with malformed bearer token     → 401
#   4. /chat with expired token              → 401
#   5. /chat with valid viewer token         → 200
#   6. /chat with valid admin token          → 200
#
# Tears the server down on EXIT. Safe to re-run; idempotent.
#
# Usage:
#   ./scripts/run_auth_demo.sh                # uses port 18080
#   ./scripts/run_auth_demo.sh --skip-chat    # auth gates only (no LLM)
#   ./scripts/run_auth_demo.sh --verbose      # full request/response bodies
#   PORT=9000 ./scripts/run_auth_demo.sh      # override port
#   KEEP_LOG=1 ./scripts/run_auth_demo.sh     # retain server log on success
#
# Requires LLM credentials in your environment (GOOGLE_API_KEY or
# Vertex AI auth) for tests 5 and 6.

set -euo pipefail

# ── Args ────────────────────────────────────────────────────────────
SKIP_CHAT=0
VERBOSE=0
for arg in "$@"; do
  case "$arg" in
    --skip-chat)  SKIP_CHAT=1 ;;
    --verbose|-v) VERBOSE=1 ;;
    -h|--help)    sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)            echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

# ── Config ──────────────────────────────────────────────────────────
PORT="${PORT:-18080}"
HOST="${HOST:-127.0.0.1}"
JWT_AUDIENCE="${JWT_AUDIENCE:-orrery-dev}"
JWT_ISSUER="${JWT_ISSUER:-https://dev.local}"
export JWT_AUDIENCE JWT_ISSUER

# Generate a fresh secret per run so old tokens don't accidentally validate.
# 64 hex chars = 32 bytes — meets PyJWT's HS256 length recommendation.
JWT_SECRET="${JWT_SECRET:-$(openssl rand -hex 32)}"
export JWT_SECRET

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SERVER_LOG="$(mktemp -t orrery-auth-demo.XXXXXX.log)"
RESP_BODY="$(mktemp -t orrery-auth-demo-body.XXXXXX)"
RESP_HEADERS="$(mktemp -t orrery-auth-demo-headers.XXXXXX)"
SERVER_PID=""
TESTS_PASSED=0
DEMO_FAILED=0

# ── Colors (only on a TTY) ──────────────────────────────────────────
if [[ -t 1 ]]; then
  GREEN=$'\033[32m'; RED=$'\033[31m'; BLUE=$'\033[34m'; YELLOW=$'\033[33m'
  DIM=$'\033[2m'; BOLD=$'\033[1m'; CYAN=$'\033[36m'; RESET=$'\033[0m'
else
  GREEN=""; RED=""; BLUE=""; YELLOW=""; DIM=""; BOLD=""; CYAN=""; RESET=""
fi

step()   { echo; echo "${BLUE}${BOLD}▶${RESET} ${BOLD}$*${RESET}"; }
pass()   { echo "  ${GREEN}✓${RESET} $*"; TESTS_PASSED=$((TESTS_PASSED + 1)); }
fail()   { echo "  ${RED}✗${RESET} $*" >&2; }
warn()   { echo "  ${YELLOW}!${RESET} $*"; }
info()   { echo "  ${DIM}·${RESET} ${DIM}$*${RESET}"; }
kv()     { printf "    ${DIM}%-14s${RESET} %s\n" "$1" "$2"; }

# ── Cleanup ─────────────────────────────────────────────────────────
cleanup() {
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  rm -f "$RESP_BODY" "$RESP_HEADERS"
  if [[ -f "$SERVER_LOG" ]]; then
    if [[ "${KEEP_LOG:-0}" == "1" || "$DEMO_FAILED" == "1" ]]; then
      echo
      echo "${DIM}Server log retained at: ${SERVER_LOG}${RESET}"
    else
      rm -f "$SERVER_LOG"
    fi
  fi
}
trap cleanup EXIT INT TERM

# ── Boot the server ─────────────────────────────────────────────────
step "Configuration"
kv "Endpoint"     "http://${HOST}:${PORT}"
kv "Audience"     "$JWT_AUDIENCE"
kv "Issuer"       "$JWT_ISSUER"
kv "Algorithm"    "HS256"
kv "JWT_SECRET"   "${JWT_SECRET:0:8}… (${#JWT_SECRET} bytes)"
kv "Server log"   "$SERVER_LOG"
[[ "$VERBOSE" == "1" ]] && kv "Mode" "verbose"

step "Booting orrery_assistant"
cd "$REPO_ROOT/agents/orrery-assistant"

AUTH_ENABLED=true \
JWT_ALGORITHM=HS256 \
ENABLE_METRICS_SERVER=false \
  uv run uvicorn orrery_assistant.app:api \
    --host "$HOST" --port "$PORT" \
    > "$SERVER_LOG" 2>&1 &
SERVER_PID=$!
info "Uvicorn pid: $SERVER_PID"

# Wait for /healthz. Up to 20s — model loading + planner init is the slow path.
start_ts=$(date +%s%N)
ready=0
for i in {1..40}; do
  if curl -sS -o /dev/null --max-time 1 "http://${HOST}:${PORT}/healthz" 2>/dev/null; then
    elapsed_ms=$(( ($(date +%s%N) - start_ts) / 1000000 ))
    pass "Server up after ${elapsed_ms}ms"
    ready=1
    break
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    DEMO_FAILED=1
    fail "Server died during startup. Last 20 log lines:"
    tail -20 "$SERVER_LOG" | sed 's/^/      /' >&2
    exit 1
  fi
  sleep 0.5
done

if [[ "$ready" == "0" ]]; then
  DEMO_FAILED=1
  fail "Server never reached ready state. Last 20 log lines:"
  tail -20 "$SERVER_LOG" | sed 's/^/      /' >&2
  exit 1
fi

# ── HTTP helper ─────────────────────────────────────────────────────
# Logs request details + response status, latency, key headers, and a
# body preview to stderr. The HTTP status code is the only thing written
# to stdout so callers can do `status=$(do_request ...)`.
#
# Stores the latest response body/headers in temp files ($RESP_BODY,
# $RESP_HEADERS) so callers can read them after the call.
#
# Usage: status=$(do_request <test_label> <method> <path> [curl args ...])
do_request() {
  local label="$1" method="$2" path="$3"
  shift 3

  # Pull a few values from the request args for logging.
  local body="(none)" token_preview="(none)"
  local -a curl_args=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -d|--data|--data-raw|--data-binary)
        body="$2"
        curl_args+=("$1" "$2")
        shift 2 ;;
      -H)
        if [[ "$2" =~ ^Authorization:[[:space:]]*Bearer[[:space:]]+(.+)$ ]]; then
          local tok="${BASH_REMATCH[1]}"
          if [[ ${#tok} -le 20 ]]; then
            token_preview="$tok"
          else
            token_preview="…${tok: -12} (${#tok} chars)"
          fi
        fi
        curl_args+=("$1" "$2")
        shift 2 ;;
      *)
        curl_args+=("$1")
        shift ;;
    esac
  done

  : > "$RESP_BODY"
  : > "$RESP_HEADERS"

  # All human-readable output goes to stderr — stdout is reserved for
  # the HTTP status code so the caller can capture it via $(...).
  {
    kv "Request"  "${method} http://${HOST}:${PORT}${path}"
    kv "Token"    "$token_preview"
    if [[ "$body" != "(none)" ]]; then
      if [[ "$VERBOSE" == "1" || ${#body} -le 80 ]]; then
        kv "Body"   "$body"
      else
        kv "Body"   "${body:0:77}… (${#body} chars)"
      fi
    fi
  } >&2

  # --max-time bounds the wait so a stuck LLM call doesn't hang the demo
  # forever. 120s covers a Gemini cold-start with a planner-enabled root.
  local fmt='%{http_code}|%{time_total}|%{size_download}'
  local result rc=0
  result=$(
    curl -sS --max-time 120 \
      -o "$RESP_BODY" -D "$RESP_HEADERS" \
      -w "$fmt" -X "$method" \
      "http://${HOST}:${PORT}${path}" "${curl_args[@]}"
  ) || rc=$?

  local status="000" time_total="?" size="?"
  if [[ -n "${result:-}" ]]; then
    IFS='|' read -r status time_total size <<<"$result"
  fi

  {
    if [[ "$rc" != "0" && "$status" == "000" ]]; then
      kv "curl exit"  "$rc (likely timeout or connection error)"
    fi

    local latency_ms="?"
    if [[ "$time_total" != "?" ]]; then
      latency_ms=$(awk -v t="$time_total" 'BEGIN{printf "%.0fms", t * 1000}')
    fi
    kv "Status"   "$status   ${DIM}(${latency_ms}, ${size}B)${RESET}"

    # Surface headers that matter for auth.
    local www_auth content_type
    www_auth=$(grep -i -m1 '^www-authenticate:' "$RESP_HEADERS" 2>/dev/null | sed 's/\r$//' || true)
    content_type=$(grep -i -m1 '^content-type:' "$RESP_HEADERS" 2>/dev/null | sed 's/\r$//' || true)
    [[ -n "$www_auth" ]] && kv "Header" "$www_auth"
    [[ -n "$content_type" && "$VERBOSE" == "1" ]] && kv "Header" "$content_type"

    # Body preview: pretty-print JSON if possible, truncate long bodies.
    if [[ -s "$RESP_BODY" ]]; then
      local body_preview
      if command -v jq >/dev/null 2>&1 && jq -e . "$RESP_BODY" >/dev/null 2>&1; then
        body_preview=$(jq -c . "$RESP_BODY")
      else
        body_preview=$(cat "$RESP_BODY")
      fi
      if [[ "$VERBOSE" != "1" && ${#body_preview} -gt 160 ]]; then
        body_preview="${body_preview:0:157}…"
      fi
      kv "Body"     "$body_preview"
    fi
  } >&2

  # Stdout: just the status code.
  echo "$status"
}

assert_status() {
  local label="$1" expected="$2" actual="$3"
  if [[ "$actual" == "$expected" ]]; then
    pass "$label → expected $expected, got $actual"
  else
    DEMO_FAILED=1
    fail "$label: expected $expected, got $actual"
    echo "    last 30 lines of server log:" >&2
    tail -30 "$SERVER_LOG" | sed 's/^/      /' >&2
    exit 1
  fi
}

mint_token() {
  local role="$1"
  shift
  uv run --project "$REPO_ROOT" python "$REPO_ROOT/scripts/dev_token.py" \
    --role "$role" \
    --audience "$JWT_AUDIENCE" \
    --issuer "$JWT_ISSUER" \
    --secret "$JWT_SECRET" \
    "$@"
}

# ── Test 1: /healthz is unauthenticated ─────────────────────────────
step "Test 1 — /healthz without auth"
status=$(do_request "/healthz" GET /healthz)
assert_status "/healthz" 200 "$status"

# ── Test 2: /chat without bearer → 401 ──────────────────────────────
step "Test 2 — /chat with no Authorization header"
status=$(do_request "/chat (no token)" POST /chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"hi"}')
assert_status "/chat (no token)" 401 "$status"

# ── Test 3: /chat with garbage token → 401 ──────────────────────────
step "Test 3 — /chat with malformed bearer token"
status=$(do_request "/chat (bad token)" POST /chat \
  -H "Authorization: Bearer not-a-real-jwt" \
  -H 'Content-Type: application/json' \
  -d '{"message":"hi"}')
assert_status "/chat (bad token)" 401 "$status"

# ── Test 4: /chat with expired token → 401 ──────────────────────────
step "Test 4 — /chat with expired token (exp 60s in the past)"
expired_token=$(mint_token admin --expires-in -60)
info "Minted expired token: …${expired_token: -12}"
status=$(do_request "/chat (expired token)" POST /chat \
  -H "Authorization: Bearer $expired_token" \
  -H 'Content-Type: application/json' \
  -d '{"message":"hi"}')
assert_status "/chat (expired token)" 401 "$status"

if [[ "$SKIP_CHAT" == "1" ]]; then
  warn "--skip-chat passed; skipping the two LLM-calling tests"
  echo
  echo "${GREEN}${BOLD}auth gates verified${RESET} ($TESTS_PASSED/5 checks)"
  exit 0
fi

# ── Test 5: viewer token → 200 ──────────────────────────────────────
# Use a trivial prompt that doesn't fan out to specialist agents — calling
# Kafka/K8s/Prometheus tools against systems that aren't running locally
# makes the demo wait for connection timeouts (30s+ each). The point here
# is to verify auth → agent dispatch, not the agent's tool behaviour.
step "Test 5 — /chat with valid viewer token  ${DIM}(may take up to 60s on cold start)${RESET}"
viewer_token=$(mint_token viewer)
info "Minted viewer token: …${viewer_token: -12}"
status=$(do_request "/chat (viewer)" POST /chat \
  -H "Authorization: Bearer $viewer_token" \
  -H 'Content-Type: application/json' \
  -d '{"message":"reply with the single word pong"}')
assert_status "/chat (viewer)" 200 "$status"
# Surface the session_id + agent reply so the user sees what came back.
if command -v jq >/dev/null 2>&1; then
  info "Session: $(jq -r '.session_id' "$RESP_BODY")"
  info "Reply  : $(jq -r '.response' "$RESP_BODY" | head -c 120)"
fi

# ── Test 6: admin token → 200 ───────────────────────────────────────
step "Test 6 — /chat with valid admin token"
admin_token=$(mint_token admin)
info "Minted admin token: …${admin_token: -12}"
status=$(do_request "/chat (admin)" POST /chat \
  -H "Authorization: Bearer $admin_token" \
  -H 'Content-Type: application/json' \
  -d '{"message":"reply with the single word ok"}')
assert_status "/chat (admin)" 200 "$status"
if command -v jq >/dev/null 2>&1; then
  info "Session: $(jq -r '.session_id' "$RESP_BODY")"
  info "Reply  : $(jq -r '.response' "$RESP_BODY" | head -c 120)"
fi

echo
echo "${GREEN}${BOLD}all $TESTS_PASSED checks passed${RESET}"
[[ "${KEEP_LOG:-0}" != "1" ]] && echo "${DIM}(set KEEP_LOG=1 to retain the server log on success)${RESET}"
