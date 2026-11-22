#!/usr/bin/env bash
set -euo pipefail

exec "$(dirname "$0")/scripts/macos_launchd_control.sh" restart
