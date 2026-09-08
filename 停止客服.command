#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
SUPERVISOR="$ROOT_DIR/wecom-gui/scripts/wecom-supervisor"
APP_PID="$(pgrep -x UDAWeComAgent || true)"

[[ -x "$SUPERVISOR" ]] || { echo "ERROR: missing supervisor: $SUPERVISOR" >&2; exit 1; }
"$SUPERVISOR" stop edge
if [[ -n "$APP_PID" ]]; then
  kill "$APP_PID" 2>/dev/null || true
fi
"$SUPERVISOR" status
