#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

cd "$ROOT_DIR"

echo "修复 Pi provider 配置"
echo "项目目录: $ROOT_DIR"
echo

./scripts/install-config.sh

PI_HOME="$HOME/.codex-csbot-wecom/pi-home"
PI_BIN="$(cd "$ROOT_DIR/wecom-gui" && set -a && . ./.env.local && set +a && printf "%s" "${WECOM_GUI_PI_COMMAND:-}")"
if [[ -z "$PI_BIN" || ! -x "$PI_BIN" ]]; then
  PI_BIN="$(command -v pi || true)"
fi
if [[ -z "$PI_BIN" ]]; then
  echo "ERROR: 未找到 pi 命令。请先运行 一键安装.command。"
  exit 1
fi

echo
echo "Pi 路径: $PI_BIN"
PI_CODING_AGENT_DIR="$PI_HOME" "$PI_BIN" --offline --list-models deepseek
PI_CODING_AGENT_DIR="$PI_HOME" "$PI_BIN" --no-session --no-extensions --no-skills --no-themes \
  --provider uda-openai --model deepseek-v4-flash --thinking off -p '只回复 OK'

echo
echo "修复完成。看到 OK 就说明 uda-openai provider 已可用。"
echo
read -r -p "按回车关闭窗口..."
