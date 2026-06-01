#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

cd "$ROOT_DIR/wecom-gui"
./scripts/wecom-agent start
if [[ "${WECOM_AGENT_MODE:-review}" == "review" ]]; then
  ./scripts/wecom-agent review-start
fi

echo
echo "已启动。日志命令："
echo "tail -f \"$ROOT_DIR/wecom-gui/.codex-run/wecom-agent.log\""
if [[ "${WECOM_AGENT_MODE:-review}" == "review" ]]; then
  echo
  echo "审核页日志："
  echo "tail -f \"$ROOT_DIR/wecom-gui/.codex-run/wecom-review.log\""
fi
echo
read -r -p "按回车关闭窗口..."
