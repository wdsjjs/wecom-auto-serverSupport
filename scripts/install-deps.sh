#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$ROOT_DIR/.venv"
MIN_PYTHON_MAJOR=3
MIN_PYTHON_MINOR=10
HOMEBREW_MIRROR="${HOMEBREW_MIRROR:-tsinghua}"
PYPI_MIRROR="${PYPI_MIRROR:-tsinghua}"

cd "$ROOT_DIR"

ensure_command_line_tools() {
  if xcode-select -p >/dev/null 2>&1; then
    return 0
  fi

  echo "Xcode Command Line Tools not found. Opening Apple's installer..."
  echo "Please finish the Command Line Tools install in the macOS popup, then press Enter here."
  xcode-select --install >/dev/null 2>&1 || true
  read -r -p "Press Enter after Command Line Tools installation is complete..."

  if ! xcode-select -p >/dev/null 2>&1; then
    echo "ERROR: Xcode Command Line Tools are still not installed." >&2
    echo "Run 'xcode-select --install' manually, finish the popup install, then rerun this script." >&2
    return 1
  fi
}

setup_homebrew_mirror_env() {
  case "$HOMEBREW_MIRROR" in
    0|false|False|FALSE|off|OFF|official)
      return 0
      ;;
    tsinghua|tuna|1|true|True|TRUE|on|ON)
      export HOMEBREW_BREW_GIT_REMOTE="${HOMEBREW_BREW_GIT_REMOTE:-https://mirrors.tuna.tsinghua.edu.cn/git/homebrew/brew.git}"
      export HOMEBREW_CORE_GIT_REMOTE="${HOMEBREW_CORE_GIT_REMOTE:-https://mirrors.tuna.tsinghua.edu.cn/git/homebrew/homebrew-core.git}"
      export HOMEBREW_API_DOMAIN="${HOMEBREW_API_DOMAIN:-https://mirrors.tuna.tsinghua.edu.cn/homebrew-bottles/api}"
      export HOMEBREW_BOTTLE_DOMAIN="${HOMEBREW_BOTTLE_DOMAIN:-https://mirrors.tuna.tsinghua.edu.cn/homebrew-bottles}"
      export HOMEBREW_INSTALL_FROM_API="${HOMEBREW_INSTALL_FROM_API:-1}"
      export HOMEBREW_PIP_INDEX_URL="${HOMEBREW_PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
      echo "Using Homebrew mirror: Tsinghua Tuna"
      ;;
    *)
      echo "ERROR: Unsupported HOMEBREW_MIRROR='$HOMEBREW_MIRROR'. Use 'tsinghua' or 'official'." >&2
      return 1
      ;;
  esac
}

setup_pip_mirror_env() {
  case "$PYPI_MIRROR" in
    0|false|False|FALSE|off|OFF|official)
      return 0
      ;;
    tsinghua|tuna|1|true|True|TRUE|on|ON)
      export PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
      echo "Using pip mirror: Tsinghua Tuna"
      ;;
    *)
      echo "ERROR: Unsupported PYPI_MIRROR='$PYPI_MIRROR'. Use 'tsinghua' or 'official'." >&2
      return 1
      ;;
  esac
}

load_homebrew_path() {
  if [[ -x "/opt/homebrew/bin/brew" ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  elif [[ -x "/usr/local/bin/brew" ]]; then
    eval "$(/usr/local/bin/brew shellenv)"
  fi
}

run_homebrew_installer() {
  if [[ "$HOMEBREW_MIRROR" == "tsinghua" || "$HOMEBREW_MIRROR" == "tuna" || "$HOMEBREW_MIRROR" == "1" || "$HOMEBREW_MIRROR" == "true" || "$HOMEBREW_MIRROR" == "True" || "$HOMEBREW_MIRROR" == "TRUE" || "$HOMEBREW_MIRROR" == "on" || "$HOMEBREW_MIRROR" == "ON" ]]; then
    local tmp_dir
    tmp_dir="$(mktemp -d)"
    git clone --depth=1 https://mirrors.tuna.tsinghua.edu.cn/git/homebrew/install.git "$tmp_dir/homebrew-install"
    /bin/bash "$tmp_dir/homebrew-install/install.sh"
    rm -rf "$tmp_dir"
  else
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  fi
}

install_homebrew() {
  ensure_command_line_tools || return 1
  setup_homebrew_mirror_env || return 1

  if command -v brew >/dev/null 2>&1; then
    return 0
  fi
  load_homebrew_path
  if command -v brew >/dev/null 2>&1; then
    return 0
  fi

  if [[ "${INSTALL_HOMEBREW:-1}" != "1" ]]; then
    return 1
  fi

  echo "Homebrew not found. Installing Homebrew..."
  echo "This may ask for the Mac login password."
  run_homebrew_installer
  load_homebrew_path

  if ! command -v brew >/dev/null 2>&1; then
    echo "ERROR: Homebrew installed but brew is not available in PATH." >&2
    echo "Open a new Terminal window or add Homebrew shellenv to your shell profile, then rerun this script." >&2
    return 1
  fi
}

python_ok() {
  local bin="$1"
  "$bin" - "$MIN_PYTHON_MAJOR" "$MIN_PYTHON_MINOR" <<'PY' >/dev/null 2>&1
import sys
need = (int(sys.argv[1]), int(sys.argv[2]))
raise SystemExit(0 if sys.version_info[:2] >= need else 1)
PY
}

python_version() {
  "$1" - <<'PY' 2>/dev/null || true
import sys
print(".".join(map(str, sys.version_info[:3])))
PY
}

find_python() {
  if [[ -n "${PYTHON_BIN:-}" ]] && command -v "$PYTHON_BIN" >/dev/null 2>&1 && python_ok "$PYTHON_BIN"; then
    command -v "$PYTHON_BIN"
    return 0
  fi
  local candidates=(
    python3.13
    python3.12
    python3.11
    python3.10
    /opt/homebrew/bin/python3.13
    /opt/homebrew/bin/python3.12
    /opt/homebrew/bin/python3.11
    /opt/homebrew/bin/python3.10
    /usr/local/bin/python3.13
    /usr/local/bin/python3.12
    /usr/local/bin/python3.11
    /usr/local/bin/python3.10
    python3
  )
  local candidate
  for candidate in "${candidates[@]}"; do
    if command -v "$candidate" >/dev/null 2>&1; then
      local resolved
      resolved="$(command -v "$candidate")"
      if python_ok "$resolved"; then
        printf "%s\n" "$resolved"
        return 0
      fi
    elif [[ -x "$candidate" ]] && python_ok "$candidate"; then
      printf "%s\n" "$candidate"
      return 0
    fi
  done
  return 1
}

install_brew_python() {
  install_homebrew || return 1
  setup_homebrew_mirror_env || return 1
  echo "Python >= ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR} not found. Installing python@3.12 via Homebrew..."
  brew install python@3.12
  load_homebrew_path
}

ensure_node_and_npm() {
  if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
    return 0
  fi
  install_homebrew || return 1
  setup_homebrew_mirror_env || return 1
  echo "Node.js/npm not found. Installing node via Homebrew..."
  brew install node
  load_homebrew_path
  if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
    echo "ERROR: Node.js/npm are still unavailable after install." >&2
    return 1
  fi
}

ensure_pi() {
  if command -v pi >/dev/null 2>&1; then
    echo "Using Pi: $(command -v pi)"
    return 0
  fi

  ensure_node_and_npm || return 1
  echo "Pi coding-agent not found. Installing @earendil-works/pi-coding-agent globally..."
  npm install -g @earendil-works/pi-coding-agent
  load_homebrew_path

  if ! command -v pi >/dev/null 2>&1; then
    echo "ERROR: pi was installed but is not available in PATH." >&2
    echo "Open a new Terminal window or add the npm global bin directory to PATH, then rerun this script." >&2
    return 1
  fi
  echo "Using Pi: $(command -v pi)"
}

ensure_command_line_tools || exit 1
setup_pip_mirror_env || exit 1

PYTHON_BIN="$(find_python || true)"
if [[ -z "$PYTHON_BIN" ]]; then
  install_brew_python || {
    echo "ERROR: Python >= ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR} is required, and Homebrew is not available." >&2
    echo "Install Homebrew from https://brew.sh, or rerun with INSTALL_HOMEBREW=1." >&2
    exit 1
  }
  PYTHON_BIN="$(find_python || true)"
fi

if [[ -z "$PYTHON_BIN" ]]; then
  echo "ERROR: Python >= ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR} is still not available after install." >&2
  exit 1
fi

echo "Using Python: $PYTHON_BIN ($(python_version "$PYTHON_BIN"))"

if [[ ! -d "$VENV_DIR" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
else
  if ! python_ok "$VENV_DIR/bin/python"; then
    echo "Existing .venv Python is too old ($(python_version "$VENV_DIR/bin/python")). Recreating .venv..."
    rm -rf "$VENV_DIR"
    "$PYTHON_BIN" -m venv "$VENV_DIR"
  fi
fi

"$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel
"$VENV_DIR/bin/python" -m pip install -e "$ROOT_DIR/wecom-gui"
"$VENV_DIR/bin/python" -m pip install "$ROOT_DIR/codex-csbot-wecom"
ensure_pi || exit 1

echo "Dependencies installed:"
echo "  $VENV_DIR/bin/python"
echo "  $(command -v pi)"
echo
echo "Run config install next:"
echo "  ./scripts/install-config.sh"
