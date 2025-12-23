#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

log() { printf "==> %s\n" "$*"; }
warn() { printf "WARN: %s\n" "$*" >&2; }
die() { printf "ERROR: %s\n" "$*" >&2; exit 1; }

assert_contains() {
  local file="$1"
  local needle="$2"
  grep -Fq "$needle" "$file" || die "Expected '$needle' in $file"
}

assert_not_contains() {
  local file="$1"
  local needle="$2"
  if grep -Fq "$needle" "$file"; then
    die "Did not expect '$needle' in $file"
  fi
}

sanitize_output() {
  tr -d '\r\004' | sed -E 's/\x1B\\[[0-9;]*[A-Za-z]//g'
}

PYTHON_BIN="${MEMORI_PYTHON_BIN:-}"
if [ -z "$PYTHON_BIN" ]; then
  if command -v python3.11 >/dev/null 2>&1; then
    PYTHON_BIN="python3.11"
  elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
  else
    die "Python not found. Set MEMORI_PYTHON_BIN to override."
  fi
fi

if ! command -v curl >/dev/null 2>&1; then
  die "curl is required for the login flow test."
fi

log "Using python: $PYTHON_BIN"

if ! "$PYTHON_BIN" - <<'PY' >/dev/null 2>&1; then
import memori  # noqa: F401
PY
  die "memori is not importable in $PYTHON_BIN. Install it first."
fi

MEMORI_PY_CMD=("$PYTHON_BIN" -m memori)
MEMORI_ENTRY_CMD=()
if command -v memori >/dev/null 2>&1; then
  MEMORI_ENTRY_CMD=(memori)
fi

TMP_DIR="$(mktemp -d)"
KEYRING_STATE_FILE="$TMP_DIR/keyring_state.json"
KEYRING_READY=0
AUTH_SIM_PID=""

cleanup() {
  if [ -n "${AUTH_SIM_PID:-}" ] && kill -0 "$AUTH_SIM_PID" >/dev/null 2>&1; then
    kill "$AUTH_SIM_PID" >/dev/null 2>&1 || true
    wait "$AUTH_SIM_PID" >/dev/null 2>&1 || true
  fi
  if [ "$KEYRING_READY" -eq 1 ] && [ -f "$KEYRING_STATE_FILE" ]; then
    "$PYTHON_BIN" - "$KEYRING_STATE_FILE" <<'PY' >/dev/null 2>&1 || true
import json
import sys

import keyring

state = json.load(open(sys.argv[1], "r"))
service = "memori"

def restore_value(username, value):
    if value is None:
        try:
            keyring.delete_password(service, username)
        except Exception:
            pass
    else:
        keyring.set_password(service, username, value)

restore_value("api_key", state.get("api_key"))
restore_value("account_email", state.get("email"))
PY
  fi
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

log "Checking CLI entrypoints"
if [ "${#MEMORI_ENTRY_CMD[@]}" -gt 0 ]; then
  "${MEMORI_ENTRY_CMD[@]}" --help >/dev/null
fi
"${MEMORI_PY_CMD[@]}" --help >/dev/null

SKIP_KEYRING="${MEMORI_SMOKE_SKIP_KEYRING:-0}"
if [ "$SKIP_KEYRING" -eq 0 ]; then
  if "$PYTHON_BIN" - <<'PY' >"$KEYRING_STATE_FILE"; then
import json
import sys

import keyring

service = "memori"
try:
    state = {
        "api_key": keyring.get_password(service, "api_key"),
        "email": keyring.get_password(service, "account_email"),
    }
    print(json.dumps(state))
except Exception as exc:
    print(f"{exc}", file=sys.stderr)
    sys.exit(1)
PY
    KEYRING_READY=1
  else
    warn "Keyring unavailable; skipping keychain login flow."
  fi
else
  warn "Skipping keychain login flow (MEMORI_SMOKE_SKIP_KEYRING=1)."
fi

MEMORI_CMD=("${MEMORI_PY_CMD[@]}")

if [ "$KEYRING_READY" -eq 1 ]; then
  log "Testing login -> status -> logout (keychain)"
  AUTH_SIM_LOG="$TMP_DIR/auth_sim.log"
  "$PYTHON_BIN" -u scripts/auth_sim_server.py --port 0 >"$AUTH_SIM_LOG" 2>&1 &
  AUTH_SIM_PID=$!
  AUTH_SIM_PORT=""
  for _ in $(seq 1 50); do
    AUTH_SIM_PORT="$(awk -F= '/AUTH_SIM_PORT=/{print $2; exit}' "$AUTH_SIM_LOG" 2>/dev/null || true)"
    if [ -n "$AUTH_SIM_PORT" ]; then
      break
    fi
    sleep 0.1
  done
  if [ -z "$AUTH_SIM_PORT" ]; then
    tail -n 20 "$AUTH_SIM_LOG" >&2 || true
    die "Auth simulator failed to start."
  fi
  AUTH_SIM_BASE="http://127.0.0.1:$AUTH_SIM_PORT"
  if ! curl -sS "$AUTH_SIM_BASE/health" >/dev/null; then
    die "Auth simulator health check failed."
  fi

  PORT="$("$PYTHON_BIN" - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)"

  LOGIN_OUT="$TMP_DIR/login.out"
  NO_COLOR=1 BROWSER=true "${MEMORI_CMD[@]}" login \
    --auth-base "$AUTH_SIM_BASE" \
    --login-url "$AUTH_SIM_BASE/login" \
    --port "$PORT" \
    --timeout 5 >"$LOGIN_OUT" 2>&1 &
  LOGIN_PID=$!

  FLOW_ID=""
  FLOW_JSON="$TMP_DIR/token_flow.json"
  for _ in $(seq 1 50); do
    if curl -sS "$AUTH_SIM_BASE/v1/token-flow/latest" >"$FLOW_JSON" 2>/dev/null; then
      FLOW_ID="$("$PYTHON_BIN" - "$FLOW_JSON" <<'PY'
import json
import sys

path = sys.argv[1]
try:
    with open(path, "r") as handle:
        data = json.load(handle)
except (OSError, json.JSONDecodeError):
    print("")
else:
    print(data.get("token_flow_id") or "")
PY
      )"
    else
      FLOW_ID=""
    fi
    if [ -n "$FLOW_ID" ]; then
      break
    fi
    sleep 0.2
  done
  if [ -z "$FLOW_ID" ]; then
    die "Token flow id was not created."
  fi

  curl -sS -X POST "$AUTH_SIM_BASE/v1/token-flow/activate" \
    -H "Content-Type: application/json" \
    -d "{\"token_flow_id\":\"$FLOW_ID\"}" >/dev/null

  wait "$LOGIN_PID"
  assert_contains "$LOGIN_OUT" "Authenticated"

  STATUS_OUT="$TMP_DIR/status.out"
  NO_COLOR=1 "${MEMORI_CMD[@]}" status >"$STATUS_OUT" 2>&1
  assert_contains "$STATUS_OUT" "Authenticated"
  assert_contains "$STATUS_OUT" "keychain"

  NO_COLOR=1 "${MEMORI_CMD[@]}" logout >/dev/null 2>&1 || true

  STATUS_OUT2="$TMP_DIR/status2.out"
  NO_COLOR=1 "${MEMORI_CMD[@]}" status >"$STATUS_OUT2" 2>&1 || true
  assert_contains "$STATUS_OUT2" "Not logged in"
fi

log "Testing env key precedence"
STATUS_ENV_OUT="$TMP_DIR/status-env.out"
MEMORI_API_KEY=env-test-key MEMORI_DISABLE_KEYRING=1 NO_COLOR=1 \
  "${MEMORI_CMD[@]}" status >"$STATUS_ENV_OUT" 2>&1
assert_contains "$STATUS_ENV_OUT" "MEMORI_API_KEY"

log "Testing quota message"
QUOTA_OUT="$TMP_DIR/quota.out"
"$PYTHON_BIN" - <<'PY' >"$QUOTA_OUT"
from memori._exceptions import QuotaExceededError

print(str(QuotaExceededError()))
PY
assert_contains "$QUOTA_OUT" 'Quota reached. Run `memori login` to upgrade.'

if command -v script >/dev/null 2>&1; then
  log "Testing auth nudge (TTY)"
  NUDGE_PY="$TMP_DIR/nudge.py"
  cat >"$NUDGE_PY" <<'PY'
import sqlite3

from memori import Memori


def conn():
    return sqlite3.connect(":memory:")


Memori(conn=conn)
print("NudgeTestComplete")
PY

  NUDGE_RAW="$TMP_DIR/nudge.raw"
  MEMORI_DISABLE_KEYRING=1 MEMORI_DISABLE_NUDGE=0 NO_COLOR=1 \
    script -q "$NUDGE_RAW" "$PYTHON_BIN" "$NUDGE_PY" >/dev/null 2>&1
  sanitize_output <"$NUDGE_RAW" >"$TMP_DIR/nudge.out"
  assert_contains "$TMP_DIR/nudge.out" "Memori is running without an API key."
  assert_contains "$TMP_DIR/nudge.out" "NudgeTestComplete"

  NUDGE_RAW2="$TMP_DIR/nudge2.raw"
  MEMORI_DISABLE_KEYRING=1 MEMORI_DISABLE_NUDGE=1 NO_COLOR=1 \
    script -q "$NUDGE_RAW2" "$PYTHON_BIN" "$NUDGE_PY" >/dev/null 2>&1
  sanitize_output <"$NUDGE_RAW2" >"$TMP_DIR/nudge2.out"
  assert_not_contains "$TMP_DIR/nudge2.out" "Memori is running without an API key."
  assert_contains "$TMP_DIR/nudge2.out" "NudgeTestComplete"
else
  warn "script not available; skipping TTY nudge checks."
fi

log "Smoke checks completed."
