#!/usr/bin/env bash
# bws_start.sh - launch Hermes WebUI with Bitwarden-backed auth and Tailscale Funnel.
# Intended entrypoint for launchd and manual terminal use.

set -Eeuo pipefail
umask 077

PROJECT_DIR="/Users/peterkjackson/Applications/hermes-webui"
LOG_FILE="$PROJECT_DIR/webui_launch.log"
ERR_FILE="$PROJECT_DIR/webui_error.log"

# launchd gets a tiny PATH; make Homebrew/user-installed tools visible.
export PATH="/opt/homebrew/bin:/usr/local/bin:/Users/peterkjackson/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$*" >> "$LOG_FILE"; }
err() { printf '[%s] ERROR: %s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$*" >> "$ERR_FILE"; }

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    err "Required command not found: $1"
    exit 127
  fi
}

cd "$PROJECT_DIR" || { err "Cannot cd to $PROJECT_DIR"; exit 1; }

SECRET_ID="${HERMES_WEBUI_PW_SECRET_ID:-}"
if [[ -z "$SECRET_ID" && -r "$HOME/.hermes/.env" ]]; then
  SECRET_ID="$(grep -E '^[[:space:]]*HERMES_WEBUI_PW_SECRET_ID=' "$HOME/.hermes/.env" | tail -n 1 | cut -d= -f2- | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")"
fi
if [[ -z "$SECRET_ID" ]]; then
  err "HERMES_WEBUI_PW_SECRET_ID is not set."
  exit 1
fi

require_cmd python3
require_cmd tailscale

# launchd does not inherit the interactive Hermes environment. Load only the
# Bitwarden service token needed by `bws`; do not source the whole file.
if [[ -z "${BWS_ACCESS_TOKEN:-}" && -r "$HOME/.hermes/.env" ]]; then
  BWS_ACCESS_TOKEN="$(python3 - <<'PY'
from pathlib import Path
for line in Path.home().joinpath('.hermes/.env').read_text().splitlines():
    line = line.strip()
    if not line or line.startswith('#') or '=' not in line:
        continue
    k, v = line.split('=', 1)
    if k.strip() == 'BWS_ACCESS_TOKEN':
        print(v.strip().strip('"').strip("'"))
        break
PY
)"
  export BWS_ACCESS_TOKEN
fi

# Source of truth: Hermes/BWS-managed HERMES_WEBUI_PW.
# The WebUI itself expects HERMES_WEBUI_PASSWORD, so this launcher maps PW -> PASSWORD.
# Do not treat a pre-existing HERMES_WEBUI_PASSWORD as authoritative; that creates
# two competing secret names and makes failures confusing.
if [[ -z "${HERMES_WEBUI_PW:-}" ]]; then
  require_cmd bws
  require_cmd jq
  HERMES_WEBUI_PW="$(bws secret get "$SECRET_ID" | jq -er '.value // empty')" || {
    err "Could not retrieve HERMES_WEBUI_PW from Bitwarden Secrets Manager."
    exit 1
  }
  export HERMES_WEBUI_PW
fi

if [[ -z "${HERMES_WEBUI_PW:-}" ]]; then
  err "HERMES_WEBUI_PW is empty after secret resolution."
  exit 1
fi

export HERMES_WEBUI_PASSWORD="$HERMES_WEBUI_PW"
# bootstrap.py normally loads repo .env and overwrites existing values. Preserve
# the BWS-resolved password so stale local .env cannot cause invalid logins.
export HERMES_WEBUI_PRESERVE_ENV=1

if [[ "${HERMES_WEBUI_DRY_RUN:-}" == "1" ]]; then
  if [[ "$HERMES_WEBUI_PASSWORD" != "$HERMES_WEBUI_PW" ]]; then
    err "Dry run failed: HERMES_WEBUI_PASSWORD does not mirror HERMES_WEBUI_PW."
    exit 1
  fi
  log "Dry run OK: HERMES_WEBUI_PW resolved and mapped to HERMES_WEBUI_PASSWORD."
  exit 0
fi

export HERMES_WEBUI_AGENT_DIR="${HERMES_WEBUI_AGENT_DIR:-$HOME/.hermes/hermes-agent}"
export HERMES_WEBUI_STATE_DIR="${HERMES_WEBUI_STATE_DIR:-$HOME/.hermes/webui}"
export HERMES_WEBUI_HOST="${HERMES_WEBUI_HOST:-0.0.0.0}"
export HERMES_WEBUI_PORT="${HERMES_WEBUI_PORT:-8787}"
export HERMES_WEBUI_TLS_INSECURE_PROBE="${HERMES_WEBUI_TLS_INSECURE_PROBE:-1}"

log "Starting Tailscale Funnel -> https+insecure://127.0.0.1:${HERMES_WEBUI_PORT}"
if ! tailscale funnel --bg --yes "https+insecure://127.0.0.1:${HERMES_WEBUI_PORT}" >> "$LOG_FILE" 2>> "$ERR_FILE"; then
  err "Tailscale Funnel start command failed; continuing to start WebUI."
fi

log "Starting Hermes WebUI on ${HERMES_WEBUI_HOST}:${HERMES_WEBUI_PORT}"
exec python3 bootstrap.py "$HERMES_WEBUI_PORT" --host "$HERMES_WEBUI_HOST" --foreground --no-browser
