#!/usr/bin/env bash
# bws_start.sh - launch Hermes WebUI with Bitwarden-backed auth and Tailscale Funnel.
# Intended entrypoint for launchd and manual terminal use.

set -Eeuo pipefail
umask 077

PROJECT_DIR="/Users/peterkjackson/Applications/hermes-webui"
LOG_FILE="$PROJECT_DIR/webui_launch.log"
ERR_FILE="$PROJECT_DIR/webui_error.log"
RUN_MARKER="${HERMES_WEBUI_RUN_MARKER:-$HOME/.hermes/webui/service-enabled}"
export HERMES_WEBUI_RUN_MARKER="$RUN_MARKER"

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

# The launchd job uses this marker as its KeepAlive condition.  Crashes and
# external signals leave it in place, so launchd restarts the WebUI.  The GUI
# Stop action removes it before shutting down, which makes an intentional quit
# persist until the launcher app arms the service again.
if [[ ! -f "$RUN_MARKER" ]]; then
  log "WebUI is intentionally stopped; run marker is absent."
  exit 0
fi

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

# Read one opaque BWS secret ID without sourcing ~/.hermes/.env, which contains
# unrelated credentials. Returns empty when the optional integration is absent.
bws_secret_id() {
  local value_var="$1"
  local id_var="${value_var}_SECRET_ID"
  local secret_id="${!id_var:-}"
  if [[ -z "$secret_id" && -r "$HOME/.hermes/.env" ]]; then
    secret_id="$(grep -E "^[[:space:]]*${id_var}=" "$HOME/.hermes/.env" | tail -n 1 | cut -d= -f2- | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")"
  fi
  printf '%s' "$secret_id"
}

# Resolve a configured BWS secret. Callers decide whether the integration is
# mandatory; WebUI authentication is mandatory, Web Push is not.
resolve_bws_secret() {
  local value_var="$1"
  local secret_id
  secret_id="$(bws_secret_id "$value_var")"
  if [[ -z "$secret_id" ]]; then
    err "${value_var}_SECRET_ID is not set."
    return 1
  fi
  require_cmd bws
  require_cmd jq
  local value
  value="$(bws secret get "$secret_id" | jq -er '.value // empty')" || {
    err "Could not retrieve ${value_var} from Bitwarden Secrets Manager."
    return 1
  }
  printf -v "$value_var" '%s' "$value"
  export "$value_var"
}

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
# Web Push is optional. An unavailable or partially configured VAPID pair must
# disable notifications, never prevent the authenticated WebUI from starting.
if [[ -n "${HERMES_WEBUI_VAPID_PUBLIC_KEY:-}" || -n "${HERMES_WEBUI_VAPID_PRIVATE_KEY:-}" ]]; then
  if [[ -z "${HERMES_WEBUI_VAPID_PUBLIC_KEY:-}" || -z "${HERMES_WEBUI_VAPID_PRIVATE_KEY:-}" ]]; then
    unset HERMES_WEBUI_VAPID_PUBLIC_KEY HERMES_WEBUI_VAPID_PRIVATE_KEY
    err "Web Push disabled: only one VAPID key was provided."
  fi
else
  vapid_public_id="$(bws_secret_id HERMES_WEBUI_VAPID_PUBLIC_KEY)"
  vapid_private_id="$(bws_secret_id HERMES_WEBUI_VAPID_PRIVATE_KEY)"
  if [[ -z "$vapid_public_id" && -z "$vapid_private_id" ]]; then
    log "Web Push disabled: VAPID secret IDs are not configured."
  elif [[ -z "$vapid_public_id" || -z "$vapid_private_id" ]]; then
    err "Web Push disabled: VAPID secret IDs are incomplete."
  elif ! resolve_bws_secret HERMES_WEBUI_VAPID_PUBLIC_KEY || ! resolve_bws_secret HERMES_WEBUI_VAPID_PRIVATE_KEY; then
    unset HERMES_WEBUI_VAPID_PUBLIC_KEY HERMES_WEBUI_VAPID_PRIVATE_KEY
    err "Web Push disabled: VAPID secrets could not be resolved."
  fi
fi
# bootstrap.py normally loads repo .env and overwrites existing values. Preserve
# the BWS-resolved password so stale local .env cannot cause invalid logins.
export HERMES_WEBUI_PRESERVE_ENV=1
export HERMES_WEBUI_AGENT_DIR="${HERMES_WEBUI_AGENT_DIR:-$HOME/.hermes/hermes-agent}"
export HERMES_WEBUI_STATE_DIR="${HERMES_WEBUI_STATE_DIR:-$HOME/.hermes/webui}"
export HERMES_WEBUI_HOST="${HERMES_WEBUI_HOST:-0.0.0.0}"
export HERMES_WEBUI_PORT="${HERMES_WEBUI_PORT:-8787}"
export HERMES_WEBUI_TLS_INSECURE_PROBE="${HERMES_WEBUI_TLS_INSECURE_PROBE:-1}"
# The GUI restart control is opt-in and uses launchd, the sole process owner.
export HERMES_WEBUI_RESTART_COMMAND="${HERMES_WEBUI_RESTART_COMMAND:-$PROJECT_DIR/scripts/macos_launchd_control.sh restart}"

if [[ "${HERMES_WEBUI_DRY_RUN:-}" == "1" ]]; then
  if [[ "$HERMES_WEBUI_PASSWORD" != "$HERMES_WEBUI_PW" ]]; then
    err "Dry run failed: HERMES_WEBUI_PASSWORD does not mirror HERMES_WEBUI_PW."
    exit 1
  fi
  log "Dry run OK: HERMES_WEBUI_PW resolved and mapped to HERMES_WEBUI_PASSWORD."
  exit 0
fi

# Launchd does not source zsh. Keep the picker aligned with the user's explicit
# path exports without loading their interactive shell or replacing manual choices.
if workspace_sync_output="$(python3 scripts/sync_zsh_workspaces.py 2>> "$ERR_FILE")"; then
  log "$workspace_sync_output"
else
  err "zsh workspace sync failed; continuing with the existing workspace list."
fi

log "Starting Tailscale Funnel -> https+insecure://127.0.0.1:${HERMES_WEBUI_PORT}"
if ! tailscale funnel --bg --yes "https+insecure://127.0.0.1:${HERMES_WEBUI_PORT}" >> "$LOG_FILE" 2>> "$ERR_FILE"; then
  err "Tailscale Funnel start command failed; continuing to start WebUI."
fi

log "Starting Hermes WebUI on ${HERMES_WEBUI_HOST}:${HERMES_WEBUI_PORT}"
exec python3 bootstrap.py "$HERMES_WEBUI_PORT" --host "$HERMES_WEBUI_HOST" --foreground --no-browser
