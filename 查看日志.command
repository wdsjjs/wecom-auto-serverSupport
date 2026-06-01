#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="$ROOT_DIR/wecom-gui/.codex-run/wecom-agent.log"

mkdir -p "$(dirname "$LOG_FILE")"
touch "$LOG_FILE"

echo "日志文件: $LOG_FILE"
echo "按 Ctrl+C 停止查看日志。"
echo
tail -f "$LOG_FILE"
