#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GUI_ENV="$ROOT_DIR/wecom-gui/.env.local"
CSBOT_DIR="${WECOM_GUI_CSBOT_DIR:-$ROOT_DIR/codex-csbot-wecom}"
PYTHON_BIN="${WECOM_GUI_CSBOT_PYTHON:-$ROOT_DIR/.venv/bin/python}"
KB_XLSX="${CSBOT_KB_XLSX:-$ROOT_DIR/AI 知识库.xlsx}"
KB_VERSION="${CSBOT_KB_VERSION:-local-$(date +%Y%m%d%H%M%S)}"
WITH_VECTOR=0

if [[ -f "$GUI_ENV" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$GUI_ENV"
  set +a
  CSBOT_DIR="${WECOM_GUI_CSBOT_DIR:-$CSBOT_DIR}"
  PYTHON_BIN="${WECOM_GUI_CSBOT_PYTHON:-$PYTHON_BIN}"
  KB_XLSX="${CSBOT_KB_XLSX:-$KB_XLSX}"
fi

for arg in "$@"; do
  case "$arg" in
    --vector)
      WITH_VECTOR=1
      ;;
    --no-vector)
      WITH_VECTOR=0
      ;;
    --xlsx=*)
      KB_XLSX="${arg#--xlsx=}"
      ;;
    --version=*)
      KB_VERSION="${arg#--version=}"
      ;;
    -h|--help)
      cat <<'USAGE'
Usage: scripts/sync-local-knowledge.sh [--vector] [--xlsx=/path/to/AI知识库.xlsx] [--version=local-xxx]

Imports the local AI knowledge workbook into csbot kb_docs/kb_aliases.
By default this updates SQL/PG only. Add --vector to also refresh Mem0/vector knowledge.
USAGE
      exit 0
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python not found or not executable: $PYTHON_BIN" >&2
  exit 1
fi
if [[ ! -f "$KB_XLSX" ]]; then
  echo "Knowledge workbook not found: $KB_XLSX" >&2
  exit 1
fi

cd "$CSBOT_DIR"
echo "Importing local knowledge workbook:"
echo "  xlsx=$KB_XLSX"
echo "  kb_version=$KB_VERSION"
echo "  backend=$([[ -n "${CSBOT_PG_DSN:-}" ]] && echo postgres || echo sqlite)"

cmd=("$PYTHON_BIN" -m csbot kb import --xlsx "$KB_XLSX" --kb-version "$KB_VERSION")
if [[ "$WITH_VECTOR" == "1" ]]; then
  cmd+=(--vector --progress)
fi

"${cmd[@]}"
