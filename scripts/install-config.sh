#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RECOMMENDED_ROOT="$HOME/Desktop/uda-codex-wecome"
GUI_DIR="$ROOT_DIR/wecom-gui"
CSBOT_DIR="$ROOT_DIR/codex-csbot-wecom"
AI_DIR="$ROOT_DIR/ai-knowledge"
AGENTS_FILE="$AI_DIR/AGENTS.md"
SHARED_ENV="$ROOT_DIR/deploy/mac.shared.env"
GUI_ENV="$GUI_DIR/.env.local"
CSBOT_ENV="$CSBOT_DIR/.env"

mkdir -p "$GUI_DIR" "$CSBOT_DIR" "$AI_DIR" "$(dirname "$SHARED_ENV")"

quote_env() {
  local value="$1"
  value="${value//\'/\'\\\'\'}"
  printf "'%s'" "$value"
}

has_key() {
  local file="$1"
  local key="$2"
  [[ -f "$file" ]] && grep -Eq "^[[:space:]]*(export[[:space:]]+)?${key}=" "$file"
}

append_if_missing() {
  local file="$1"
  local key="$2"
  local value="$3"
  touch "$file"
  if ! has_key "$file" "$key"; then
    printf "%s=%s\n" "$key" "$value" >> "$file"
  fi
}

set_or_replace_key() {
  local file="$1"
  local key="$2"
  local value="$3"
  touch "$file"
  if has_key "$file" "$key"; then
    local tmp
    tmp="$(mktemp)"
    awk -v key="$key" -v line="${key}=${value}" '
      BEGIN { replaced = 0 }
      $0 ~ "^[[:space:]]*(export[[:space:]]+)?" key "=" {
        if (!replaced) {
          print line
          replaced = 1
        }
        next
      }
      { print }
      END {
        if (!replaced) {
          print line
        }
      }
    ' "$file" > "$tmp"
    mv "$tmp" "$file"
  else
    printf "%s=%s\n" "$key" "$value" >> "$file"
  fi
}

copy_shared_key() {
  local target="$1"
  local key="$2"
  [[ -f "$SHARED_ENV" ]] || return 0
  if has_key "$target" "$key"; then
    return 0
  fi
  local line
  line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$SHARED_ENV" | tail -n 1 || true)"
  if [[ -n "$line" ]]; then
    printf "%s\n" "$line" >> "$target"
  fi
}

detect_python() {
  if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    printf "%s\n" "$ROOT_DIR/.venv/bin/python"
  elif command -v python3.13 >/dev/null 2>&1; then
    command -v python3.13
  elif command -v python3.12 >/dev/null 2>&1; then
    command -v python3.12
  elif command -v python3.11 >/dev/null 2>&1; then
    command -v python3.11
  elif command -v python3.10 >/dev/null 2>&1; then
    command -v python3.10
  elif [[ -x "/opt/homebrew/bin/python3.12" ]]; then
    printf "%s\n" "/opt/homebrew/bin/python3.12"
  elif [[ -x "/usr/local/bin/python3.12" ]]; then
    printf "%s\n" "/usr/local/bin/python3.12"
  elif command -v python3 >/dev/null 2>&1; then
    command -v python3
  else
    printf "%s\n" "/opt/homebrew/bin/python3"
  fi
}

detect_pi() {
  local existing="$1"
  if command -v pi >/dev/null 2>&1; then
    command -v pi
  elif [[ -n "$existing" && -x "$existing" ]]; then
    printf "%s\n" "$existing"
  elif [[ -x "/opt/homebrew/bin/pi" ]]; then
    printf "%s\n" "/opt/homebrew/bin/pi"
  elif [[ -x "/usr/local/bin/pi" ]]; then
    printf "%s\n" "/usr/local/bin/pi"
  else
    printf "%s\n" "pi"
  fi
}

mask_value() {
  local key="$1"
  local value="$2"
  case "$key" in
    *KEY*|*SECRET*|*TOKEN*|*PASS*|*WEBHOOK*|*DSN*) [[ -n "$value" ]] && printf "<set>" || printf "<unset>" ;;
    *) printf "%s" "$value" ;;
  esac
}

read_env_value() {
  local file="$1"
  local key="$2"
  [[ -f "$file" ]] || return 0
  local line
  line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$file" | tail -n 1 || true)"
  [[ -n "$line" ]] || return 0
  line="${line#*=}"
  line="${line%$'\r'}"
  line="${line#\'}"
  line="${line%\'}"
  line="${line#\"}"
  line="${line%\"}"
  printf "%s" "$line"
}

write_pi_config() {
  local pi_home="$1"
  local provider="$2"
  local model="$3"
  local api_key="$4"
  local base_url="${5:-https://new-api.uda.cn/v1}"
  local image_model="${6:-}"

  mkdir -p "$pi_home"
  if [[ -z "$provider" || -z "$model" || -z "$api_key" ]]; then
    printf "WARN: Pi provider config skipped; missing provider/model/api key.\n"
    return 0
  fi

  PI_HOME="$pi_home" PI_PROVIDER="$provider" PI_MODEL="$model" PI_IMAGE_MODEL="$image_model" PI_API_KEY="$api_key" PI_BASE_URL="$base_url" \
    python3 - <<'PY'
import json
import os
from pathlib import Path

home = Path(os.environ["PI_HOME"]).expanduser()
provider = os.environ["PI_PROVIDER"]
model = os.environ["PI_MODEL"]
image_model = os.environ.get("PI_IMAGE_MODEL", "").strip()
api_key = os.environ["PI_API_KEY"]
base_url = os.environ["PI_BASE_URL"].rstrip("/")

settings_path = home / "settings.json"
models_path = home / "models.json"
auth_path = home / "auth.json"

settings = {}
if settings_path.exists():
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception:
        settings = {}
settings.update(
    {
        "defaultProvider": provider,
        "defaultModel": model,
        "defaultThinkingLevel": os.environ.get("WECOM_GUI_PI_THINKING", "off"),
    }
)

models = {}
if models_path.exists():
    try:
        models = json.loads(models_path.read_text(encoding="utf-8"))
    except Exception:
        models = {}
providers = models.setdefault("providers", {})
model_entries = [
    {
        "id": model,
        "name": model,
        "reasoning": False,
        "input": ["text"],
        "contextWindow": 128000,
        "maxTokens": 8192,
    }
]
if image_model and image_model != model:
    model_entries.append(
        {
            "id": image_model,
            "name": image_model,
            "reasoning": False,
            "input": ["text", "image"],
            "contextWindow": 128000,
            "maxTokens": 8192,
        }
    )
elif image_model == model:
    model_entries[0]["input"] = ["text", "image"]

providers[provider] = {
    "name": "UDA OpenAI Compatible",
    "baseUrl": base_url,
    "api": "openai-completions",
    "apiKey": api_key,
    "authHeader": True,
    "compat": {
        "supportsDeveloperRole": False,
        "supportsReasoningEffort": False,
        "supportsUsageInStreaming": False,
        "maxTokensField": "max_tokens",
    },
    "models": model_entries,
}

home.mkdir(parents=True, exist_ok=True)
settings_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
models_path.write_text(json.dumps(models, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
if not auth_path.exists():
    auth_path.write_text("{}\n", encoding="utf-8")
for path in (settings_path, models_path, auth_path):
    try:
        path.chmod(0o600)
    except OSError:
        pass
print(f"OK: Pi provider config {provider} -> {models_path}")
PY
}

check_path() {
  local label="$1"
  local path="$2"
  if [[ -e "$path" ]]; then
    printf "OK: %s %s\n" "$label" "$path"
  else
    printf "WARN: missing %s %s\n" "$label" "$path"
  fi
}

check_cmd() {
  local name="$1"
  if command -v "$name" >/dev/null 2>&1; then
    printf "OK: command %s\n" "$name"
  else
    printf "WARN: command missing: %s\n" "$name"
  fi
}

rewrite_agents_paths() {
  local file="$1"
  [[ -f "$file" ]] || return 0
  local tmp
  tmp="$(mktemp)"
  sed -E \
    -e "s#/Users/[^[:space:]\"'，。；；、<>]*/[^[:space:]\"'，。；；、<>]*/uda-codex-wecome#$ROOT_DIR#g" \
    -e "s#/Users/[^[:space:]\"'，。；；、<>]*/Desktop/ai-knowledge#$AI_DIR#g" \
    -e "s#/Users/[^[:space:]\"'，。；；、<>]*/\\.codex-csbot-wecom#$HOME/.codex-csbot-wecom#g" \
    "$file" > "$tmp"
  if ! cmp -s "$file" "$tmp"; then
    cp "$file" "$file.bak.$(date '+%Y%m%d%H%M%S')"
    mv "$tmp" "$file"
    printf "OK: rewrote local paths in %s\n" "$file"
  else
    rm -f "$tmp"
  fi
}

touch "$GUI_ENV" "$CSBOT_ENV"

PYTHON_BIN="$(detect_python)"
rewrite_agents_paths "$AGENTS_FILE"

for key in \
  WECOM_GUI_UDA_API_KEY WECOM_GUI_UDA_URL WECOM_GUI_APP_NAME WECOM_GUI_AI_PROVIDER \
  WECOM_GUI_CODEX_BACKEND WECOM_GUI_CODEX_COMMAND WECOM_GUI_CODEX_TIMEOUT \
  WECOM_GUI_PI_COMMAND WECOM_GUI_PI_PROVIDER WECOM_GUI_PI_MODEL WECOM_GUI_PI_TEXT_MODEL \
  WECOM_GUI_PI_IMAGE_MODEL WECOM_GUI_PI_THINKING WECOM_GUI_PI_API_KEY \
  WECOM_GUI_PI_TIMEOUT WECOM_AGENT_LAST WECOM_GUI_CSBOT_CUSTOMER_ID WECOM_GUI_PYTHONPATH \
  WECOM_AGENT_MODE \
  WECOM_GUI_REQUIRED_TAGS WECOM_GUI_REQUIRED_TAG WECOM_GUI_ENABLE_OCR_SCAN \
  WECOM_GUI_NORMALIZE_FULLSCREEN WECOM_GUI_WINDOW_MODE WECOM_GUI_COORD_MODE \
  CSBOT_MEM0_URL CSBOT_MEM0_API_KEY CSBOT_MEM0_GLOBAL_USER_ID \
  CSBOT_PG_DSN FEISHU_APP_ID FEISHU_APP_SECRET FEISHU_APP_TOKEN \
  WEIBAN_BASE_URL WEIBAN_CORP_ID WEIBAN_SECRET \
  CSBOT_FEISHU_ENABLED CSBOT_FEISHU_WEBHOOK WECOM_AGENT_SCAN_PAGES WECOM_AGENT_SCROLL_TICKS \
  WECOM_AGENT_MAX_DRAFTS WECOM_AGENT_DEEP_SCAN_INTERVAL WEWORK_CORP_ID WEWORK_AGENT_ID \
  WEWORK_API_BASE_URL WEWORK_AGENT_SECRET; do
  copy_shared_key "$GUI_ENV" "$key"
done

set_or_replace_key "$GUI_ENV" "WECOM_GUI_PYTHON" "$(quote_env "$PYTHON_BIN")"
set_or_replace_key "$GUI_ENV" "WECOM_GUI_CSBOT_PYTHON" "$(quote_env "$PYTHON_BIN")"
set_or_replace_key "$GUI_ENV" "WECOM_GUI_CSBOT_DIR" "$(quote_env "$CSBOT_DIR")"
set_or_replace_key "$GUI_ENV" "WECOM_GUI_PI_HOME" "$(quote_env "$HOME/.codex-csbot-wecom/pi-home")"
PI_BIN="$(detect_pi "$(read_env_value "$GUI_ENV" "WECOM_GUI_PI_COMMAND")")"
set_or_replace_key "$GUI_ENV" "WECOM_GUI_PI_COMMAND" "$(quote_env "$PI_BIN")"
set_or_replace_key "$GUI_ENV" "CSBOT_CODEX_WORKDIR" "$(quote_env "$AI_DIR")"
set_or_replace_key "$GUI_ENV" "CSBOT_KB_XLSX" "$(quote_env "$ROOT_DIR/AI 知识库.xlsx")"
set_or_replace_key "$GUI_ENV" "CSBOT_DB" "$(quote_env "$HOME/.codex-csbot-wecom/state.sqlite")"

PI_HOME_VALUE="$(read_env_value "$GUI_ENV" "WECOM_GUI_PI_HOME")"
PI_PROVIDER_VALUE="$(read_env_value "$GUI_ENV" "WECOM_GUI_PI_PROVIDER")"
PI_MODEL_VALUE="$(read_env_value "$GUI_ENV" "WECOM_GUI_PI_MODEL")"
if [[ -z "$PI_MODEL_VALUE" ]]; then
  PI_MODEL_VALUE="$(read_env_value "$GUI_ENV" "WECOM_GUI_PI_TEXT_MODEL")"
fi
PI_IMAGE_MODEL_VALUE="$(read_env_value "$GUI_ENV" "WECOM_GUI_PI_IMAGE_MODEL")"
PI_API_KEY_VALUE="$(read_env_value "$GUI_ENV" "WECOM_GUI_PI_API_KEY")"
if [[ -z "$PI_API_KEY_VALUE" ]]; then
  PI_API_KEY_VALUE="$(read_env_value "$GUI_ENV" "OPENAI_API_KEY")"
fi
write_pi_config "$PI_HOME_VALUE" "$PI_PROVIDER_VALUE" "$PI_MODEL_VALUE" "$PI_API_KEY_VALUE" "https://new-api.uda.cn/v1" "$PI_IMAGE_MODEL_VALUE"

for key in \
  CH_HOST CH_PORT CH_DATABASE CH_USER CH_PASS CSBOT_FEISHU_ENABLED CSBOT_FEISHU_WEBHOOK \
  CSBOT_MEM0_URL CSBOT_MEM0_API_KEY CSBOT_MEM0_GLOBAL_USER_ID \
  CSBOT_PG_DSN FEISHU_APP_ID FEISHU_APP_SECRET FEISHU_APP_TOKEN \
  WEIBAN_BASE_URL WEIBAN_CORP_ID WEIBAN_SECRET; do
  copy_shared_key "$CSBOT_ENV" "$key"
done

set_or_replace_key "$CSBOT_ENV" "CSBOT_CODEX_WORKDIR" "$(quote_env "$AI_DIR")"
set_or_replace_key "$CSBOT_ENV" "CSBOT_KB_XLSX" "$(quote_env "$ROOT_DIR/AI 知识库.xlsx")"
set_or_replace_key "$CSBOT_ENV" "CSBOT_DB" "$(quote_env "$HOME/.codex-csbot-wecom/state.sqlite")"

chmod 600 "$GUI_ENV" "$CSBOT_ENV"

check_path "wecom-gui" "$GUI_DIR"
check_path "codex-csbot-wecom" "$CSBOT_DIR"
check_path "knowledge workbook" "$ROOT_DIR/AI 知识库.xlsx"
check_path "ai-knowledge AGENTS.md" "$AGENTS_FILE"

check_cmd python3
check_cmd screen
check_cmd swift
check_cmd pi

if [[ ! -x "$ROOT_DIR/.venv/bin/python" ]]; then
  printf "WARN: .venv not found; using %s. Install dependencies if imports fail.\n" "$PYTHON_BIN"
fi

if [[ "$ROOT_DIR" != "$RECOMMENDED_ROOT" ]]; then
  printf "WARN: recommended desktop path is %s\n" "$RECOMMENDED_ROOT"
  printf "      current path is %s; this is supported because config paths were rewritten.\n" "$ROOT_DIR"
fi

printf "\nConfig written:\n  %s\n  %s\n\n" "$GUI_ENV" "$CSBOT_ENV"
printf "Key summary:\n"
for item in \
  "WECOM_GUI_AI_PROVIDER:$GUI_ENV" \
  "WECOM_GUI_PYTHON:$GUI_ENV" \
  "WECOM_GUI_CSBOT_DIR:$GUI_ENV" \
  "WECOM_AGENT_MAX_DRAFTS:$GUI_ENV" \
  "WECOM_AGENT_SCROLL_TICKS:$GUI_ENV" \
  "WECOM_GUI_UDA_API_KEY:$GUI_ENV" \
  "WECOM_GUI_PI_PROVIDER:$GUI_ENV" \
  "WECOM_GUI_PI_MODEL:$GUI_ENV" \
  "WECOM_GUI_PI_COMMAND:$GUI_ENV" \
  "WECOM_GUI_PI_HOME:$GUI_ENV" \
  "WECOM_GUI_PI_API_KEY:$GUI_ENV" \
  "CSBOT_PG_DSN:$CSBOT_ENV" \
  "FEISHU_APP_ID:$CSBOT_ENV" \
  "FEISHU_APP_SECRET:$CSBOT_ENV" \
  "FEISHU_APP_TOKEN:$CSBOT_ENV" \
  "WEIBAN_BASE_URL:$CSBOT_ENV" \
  "WEIBAN_CORP_ID:$CSBOT_ENV" \
  "WEIBAN_SECRET:$CSBOT_ENV" \
  "CSBOT_MEM0_URL:$GUI_ENV" \
  "CSBOT_MEM0_API_KEY:$GUI_ENV" \
  "CSBOT_FEISHU_WEBHOOK:$GUI_ENV" \
  "WEWORK_AGENT_SECRET:$GUI_ENV" \
  "CH_PASS:$CSBOT_ENV"; do
  key="${item%%:*}"
  file="${item#*:}"
  value="$(read_env_value "$file" "$key")"
  printf "  %s=%s\n" "$key" "$(mask_value "$key" "$value")"
done

printf "\nNext:\n"
printf "  cd %s/wecom-gui\n" "$ROOT_DIR"
printf "  ./scripts/wecom-agent start\n"
