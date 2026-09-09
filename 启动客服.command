#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
GUI_DIR="$ROOT_DIR/wecom-gui"
SUPERVISOR="$GUI_DIR/scripts/wecom-supervisor"
APP="$HOME/Applications/UDA WeCom Agent.app"

[[ -x "$SUPERVISOR" ]] || { echo "ERROR: missing supervisor: $SUPERVISOR" >&2; exit 1; }

if [[ ! -x "$APP/Contents/MacOS/UDAWeComAgent" ]]; then
  "$GUI_DIR/scripts/install-desktop-client"
else
  open "$APP"
fi

# The unified launcher owns both the status UI and the real WeCom edge worker.
# Keep the worker lifecycle in the supervisor so repeated launches are idempotent.
"$SUPERVISOR" start edge

echo "UDA WeCom Agent 和企微边缘通道已启动。"
"$SUPERVISOR" status
