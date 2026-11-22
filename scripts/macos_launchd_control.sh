#!/usr/bin/env bash
# Single, PID-safe control path for the local launchd-owned Hermes WebUI.

set -Eeuo pipefail
umask 077

LABEL="${HERMES_WEBUI_LAUNCHD_LABEL:-com.peterkjackson.hermes-webui}"
STATE_DIR="${HERMES_WEBUI_STATE_DIR:-$HOME/.hermes/webui}"
RUN_MARKER="${HERMES_WEBUI_RUN_MARKER:-$STATE_DIR/service-enabled}"
PLIST="${HERMES_WEBUI_LAUNCHD_PLIST:-$HOME/Library/LaunchAgents/$LABEL.plist}"
LOG_FILE="${HERMES_WEBUI_CONTROL_LOG:-$STATE_DIR/service-control.log}"
DOMAIN="gui/$(id -u)"
SERVICE="$DOMAIN/$LABEL"

log() {
  mkdir -p "$STATE_DIR"
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$*" >> "$LOG_FILE"
}

arm() {
  mkdir -p "$STATE_DIR"
  local pending
  pending="$(mktemp "$STATE_DIR/.service-enabled.XXXXXX")"
  printf 'enabled\n' > "$pending"
  chmod 600 "$pending"
  mv -f "$pending" "$RUN_MARKER"
  log "Armed persistent WebUI service."
}

ensure_loaded() {
  if launchctl print "$SERVICE" >/dev/null 2>&1; then
    return 0
  fi
  if [[ ! -f "$PLIST" ]]; then
    log "Cannot load WebUI service: plist is missing."
    return 1
  fi
  launchctl bootstrap "$DOMAIN" "$PLIST"
}

case "${1:-restart}" in
  arm)
    arm
    ;;
  disarm)
    rm -f "$RUN_MARKER"
    log "Disarmed persistent WebUI service."
    ;;
  start|restart)
    arm
    ensure_loaded
    launchctl kickstart -k "$SERVICE"
    log "Requested launchd restart for $LABEL."
    ;;
  status)
    if [[ -f "$RUN_MARKER" ]]; then
      printf 'enabled\n'
    else
      printf 'intentionally-stopped\n'
    fi
    launchctl print "$SERVICE"
    ;;
  *)
    printf 'Usage: %s {start|restart|status|arm|disarm}\n' "$0" >&2
    exit 64
    ;;
esac
