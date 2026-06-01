#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

cd "$ROOT_DIR/wecom-gui"
./scripts/wecom-agent stop

pkill -f 'cli_anything\.wecom_gui agent' 2>/dev/null || true
pkill -f 'SCREEN -dmS wecom-agent' 2>/dev/null || true

echo
echo "已停止。"
echo
read -r -p "按回车关闭窗口..."
