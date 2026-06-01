#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

cd "$ROOT_DIR/wecom-gui"
./scripts/wecom-agent start

echo
echo "已启动。日志命令："
echo "tail -f \"$ROOT_DIR/wecom-gui/.codex-run/wecom-agent.log\""
echo
read -r -p "按回车关闭窗口..."
