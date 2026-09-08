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

echo "UDA WeCom Agent 已启动。"
echo "边缘通道默认不启动，请在 Dock 客户端菜单中手动启动。"
