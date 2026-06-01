#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

cd "$ROOT_DIR"

echo "UDA WeCom Agent 一键安装"
echo "项目目录: $ROOT_DIR"
echo

echo "1/4 安装 Python/Node/Pi 依赖..."
./scripts/install-deps.sh
echo

echo "2/4 写入本机配置和 Pi provider..."
./scripts/install-config.sh
echo

echo "3/4 验证 Pi provider..."
PI_HOME="$HOME/.codex-csbot-wecom/pi-home"
PI_BIN="$(cd "$ROOT_DIR/wecom-gui" && set -a && . ./.env.local && set +a && printf "%s" "${WECOM_GUI_PI_COMMAND:-}")"
if [[ -z "$PI_BIN" || ! -x "$PI_BIN" ]]; then
  PI_BIN="$(command -v pi || true)"
fi
if [[ -z "$PI_BIN" ]]; then
  echo "ERROR: 未找到 pi 命令。"
  exit 1
fi
echo "Pi 路径: $PI_BIN"
PI_CODING_AGENT_DIR="$PI_HOME" "$PI_BIN" --offline --list-models deepseek
PI_CODING_AGENT_DIR="$PI_HOME" "$PI_BIN" --no-session --no-extensions --no-skills --no-themes \
  --provider uda-openai --model deepseek-v4-flash --thinking off -p '只回复 OK'
echo

echo "4/4 验证本地 CLI..."
(cd "$ROOT_DIR/wecom-gui" && ./scripts/wecom-agent status)
(cd "$ROOT_DIR/wecom-gui" && set -a && . ./.env.local && set +a && PYTHONPATH="${WECOM_GUI_PYTHONPATH:-}" "${WECOM_GUI_PYTHON:-python3}" -m cli_anything.wecom_gui --json doctor)
echo

echo "安装完成。"
echo
echo "启动自动回复："
echo "  cd \"$ROOT_DIR/wecom-gui\""
echo "  ./scripts/wecom-agent start"
echo
echo "查看日志："
echo "  tail -f \"$ROOT_DIR/wecom-gui/.codex-run/wecom-agent.log\""
echo
read -r -p "按回车关闭窗口..."
