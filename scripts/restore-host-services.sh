#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUNDLE_DIR="${1:-$ROOT_DIR/deploy/migration}"
PG_VERSION="${PG_VERSION:-16}"
PG_SERVICE="postgresql@${PG_VERSION}"
CSBOT_DB="${CSBOT_PG_DATABASE:-csbot_wecom}"
CSBOT_USER="${CSBOT_PG_USER:-csbot_app}"
CSBOT_PASSWORD="${CSBOT_PG_PASSWORD:-xvcV6THkqNgeIFG5FySxCFI1zxrsbupK}"
MEM0_DB="${MEM0_APP_DB_NAME:-mem0_app}"
MEM0_PORT="${MEM0_PORT:-8888}"
MEM0_HOME="$HOME/Library/Application Support/uda-mem0"
MEM0_TARGET="${MEM0_SERVER_DIR:-$HOME/Desktop/udaItem/mem0/server}"
SHARED_ENV="$ROOT_DIR/deploy/mac.shared.env"

load_homebrew_path() {
  if [[ -x "/opt/homebrew/bin/brew" ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  elif [[ -x "/usr/local/bin/brew" ]]; then
    eval "$(/usr/local/bin/brew shellenv)"
  fi
}

detect_lan_cidr() {
  local ip
  ip="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
  if [[ -z "$ip" ]]; then
    printf "192.168.0.0/16\n"
    return 0
  fi
  IFS=. read -r a b c _ <<<"$ip"
  printf "%s.%s.%s.0/24\n" "$a" "$b" "$c"
}

postgres_var_dir() {
  if [[ -d "/opt/homebrew/var/${PG_SERVICE}" ]]; then
    printf "/opt/homebrew/var/%s\n" "$PG_SERVICE"
  elif [[ -d "/usr/local/var/${PG_SERVICE}" ]]; then
    printf "/usr/local/var/%s\n" "$PG_SERVICE"
  else
    local prefix
    prefix="$(brew --prefix)"
    printf "%s/var/%s\n" "$prefix" "$PG_SERVICE"
  fi
}

sql_literal() {
  local value="$1"
  value="${value//\'/\'\'}"
  printf "'%s'" "$value"
}

sql_ident() {
  local value="$1"
  value="${value//\"/\"\"}"
  printf '"%s"' "$value"
}

require_file() {
  if [[ ! -e "$1" ]]; then
    echo "ERROR: missing required path: $1" >&2
    exit 1
  fi
}

replace_or_append_env() {
  local file="$1"
  local key="$2"
  local value="$3"
  touch "$file"
  if grep -Eq "^[[:space:]]*${key}=" "$file"; then
    python3 - "$file" "$key" "$value" <<'PY'
import sys
path, key, value = sys.argv[1:4]
lines = open(path, encoding="utf-8").read().splitlines()
with open(path, "w", encoding="utf-8") as f:
    for line in lines:
        if line.strip().startswith(key + "="):
            f.write(f"{key}='{value}'\n")
        else:
            f.write(line + "\n")
PY
  else
    printf "%s='%s'\n" "$key" "$value" >> "$file"
  fi
}

require_file "$BUNDLE_DIR/${CSBOT_DB}.dump"
require_file "$BUNDLE_DIR/${MEM0_DB}.dump"
require_file "$BUNDLE_DIR/mem0-server.tar.gz"
require_file "$BUNDLE_DIR/mem0-api.env"

load_homebrew_path
if ! command -v brew >/dev/null 2>&1; then
  echo "ERROR: Homebrew is required. Run ./scripts/install-deps.sh first." >&2
  exit 1
fi

brew list "$PG_SERVICE" >/dev/null 2>&1 || brew install "$PG_SERVICE"
brew services start "$PG_SERVICE"

PSQL="$(brew --prefix "$PG_SERVICE")/bin/psql"
PG_RESTORE="$(brew --prefix "$PG_SERVICE")/bin/pg_restore"
CREATEDB="$(brew --prefix "$PG_SERVICE")/bin/createdb"
DROPDB="$(brew --prefix "$PG_SERVICE")/bin/dropdb"

for _ in {1..30}; do
  if "$PSQL" -d postgres -Atc "SELECT 1" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! "$PSQL" -d postgres -Atc "SELECT 1" >/dev/null 2>&1; then
  echo "ERROR: PostgreSQL did not become ready." >&2
  exit 1
fi

USER_SQL="$(sql_literal "$CSBOT_USER")"
USER_IDENT="$(sql_ident "$CSBOT_USER")"
PASS_SQL="$(sql_literal "$CSBOT_PASSWORD")"
if ! "$PSQL" -d postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname = $USER_SQL" | grep -q 1; then
  "$PSQL" -d postgres -v ON_ERROR_STOP=1 -c "CREATE ROLE $USER_IDENT LOGIN PASSWORD $PASS_SQL;"
else
  "$PSQL" -d postgres -v ON_ERROR_STOP=1 -c "ALTER ROLE $USER_IDENT WITH LOGIN PASSWORD $PASS_SQL;"
fi

restore_db() {
  local db="$1"
  local dump="$2"
  local owner="$3"
  "$DROPDB" --if-exists "$db"
  "$CREATEDB" -O "$owner" "$db"
  "$PG_RESTORE" --clean --if-exists --no-owner --role="$owner" -d "$db" "$dump"
  "$PSQL" -d "$db" -v ON_ERROR_STOP=1 -c "GRANT ALL ON SCHEMA public TO $(sql_ident "$owner");"
}

restore_db "$CSBOT_DB" "$BUNDLE_DIR/${CSBOT_DB}.dump" "$CSBOT_USER"

if ! "$PSQL" -d postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname = '$(whoami)'" | grep -q 1; then
  "$PSQL" -d postgres -v ON_ERROR_STOP=1 -c "CREATE ROLE $(sql_ident "$(whoami)") LOGIN SUPERUSER;"
fi
restore_db "$MEM0_DB" "$BUNDLE_DIR/${MEM0_DB}.dump" "$(whoami)"

DATA_DIR="$(postgres_var_dir)"
CIDR="$(detect_lan_cidr)"
if ! grep -q "UDA CSBOT managed listen_addresses" "$DATA_DIR/postgresql.conf"; then
  {
    echo ""
    echo "# UDA CSBOT managed listen_addresses"
    echo "listen_addresses = '*'"
  } >> "$DATA_DIR/postgresql.conf"
fi
if ! grep -q "UDA CSBOT managed LAN access" "$DATA_DIR/pg_hba.conf"; then
  {
    echo ""
    echo "# UDA CSBOT managed LAN access"
    echo "host    $CSBOT_DB    $CSBOT_USER    $CIDR    scram-sha-256"
  } >> "$DATA_DIR/pg_hba.conf"
fi
brew services restart "$PG_SERVICE"

mkdir -p "$(dirname "$MEM0_TARGET")"
rm -rf "$MEM0_TARGET"
tar -xzf "$BUNDLE_DIR/mem0-server.tar.gz" -C "$(dirname "$MEM0_TARGET")"
if [[ "$(basename "$MEM0_TARGET")" != "server" && -d "$(dirname "$MEM0_TARGET")/server" ]]; then
  mv "$(dirname "$MEM0_TARGET")/server" "$MEM0_TARGET"
fi

python3 -m venv "$MEM0_TARGET/.venv"
"$MEM0_TARGET/.venv/bin/python" -m pip install --upgrade pip setuptools wheel
"$MEM0_TARGET/.venv/bin/python" -m pip install -r "$MEM0_TARGET/requirements.txt"

mkdir -p "$MEM0_HOME/logs" "$MEM0_HOME/run"
cp "$BUNDLE_DIR/mem0-api.env" "$MEM0_HOME/mem0-api.env"
chmod 600 "$MEM0_HOME/mem0-api.env"

python3 - "$MEM0_HOME/mem0-api.env" "$MEM0_TARGET" "$MEM0_DB" <<'PY'
import sys
path, target, db = sys.argv[1:4]
replacements = {
    "POSTGRES_HOST": "localhost",
    "POSTGRES_PORT": "5432",
    "POSTGRES_DB": "postgres",
    "POSTGRES_USER": __import__("getpass").getuser(),
    "POSTGRES_PASSWORD": "",
    "APP_DB_NAME": db,
    "MEM0_QDRANT_PATH": f"{target}/history/qdrant",
    "HISTORY_DB_PATH": f"{target}/history/history.db",
}
lines = open(path, encoding="utf-8").read().splitlines()
seen = set()
out = []
for line in lines:
    if "=" in line and not line.lstrip().startswith("#"):
        key = line.split("=", 1)[0].strip()
        if key in replacements:
            out.append(f"{key}={replacements[key]}")
            seen.add(key)
            continue
    out.append(line)
for key, value in replacements.items():
    if key not in seen:
        out.append(f"{key}={value}")
open(path, "w", encoding="utf-8").write("\n".join(out) + "\n")
PY

PLIST="$HOME/Library/LaunchAgents/com.uda.mem0-api.plist"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.uda.mem0-api</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-lc</string>
    <string>set -a; source "$MEM0_HOME/mem0-api.env"; set +a; unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy NO_PROXY no_proxy; cd "$MEM0_TARGET"; exec "$MEM0_TARGET/.venv/bin/uvicorn" main:app --host "0.0.0.0" --port "$MEM0_PORT"</string>
  </array>
  <key>WorkingDirectory</key><string>$MEM0_HOME</string>
  <key>StandardOutPath</key><string>$MEM0_HOME/logs/mem0-api.log</string>
  <key>StandardErrorPath</key><string>$MEM0_HOME/logs/mem0-api.err.log</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart -k "gui/$(id -u)/com.uda.mem0-api"

HOST_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || hostname)"
PG_DSN="postgresql://${CSBOT_USER}:${CSBOT_PASSWORD}@${HOST_IP}:5432/${CSBOT_DB}"
MEM0_URL="http://${HOST_IP}:${MEM0_PORT}"
MEM0_KEY="$(grep -E '^ADMIN_API_KEY=' "$MEM0_HOME/mem0-api.env" | tail -1 | cut -d= -f2-)"

mkdir -p "$(dirname "$SHARED_ENV")"
chmod 700 "$(dirname "$SHARED_ENV")"
replace_or_append_env "$SHARED_ENV" "CSBOT_PG_DSN" "$PG_DSN"
replace_or_append_env "$SHARED_ENV" "CSBOT_MEM0_URL" "$MEM0_URL"
replace_or_append_env "$SHARED_ENV" "CSBOT_MEM0_API_KEY" "$MEM0_KEY"
replace_or_append_env "$SHARED_ENV" "CSBOT_MEM0_GLOBAL_USER_ID" "global-kb"
chmod 600 "$SHARED_ENV"

echo "Restore complete."
echo "  PG DSN: $PG_DSN"
echo "  MEM0 URL: $MEM0_URL"
echo "  MEM0 key: $MEM0_KEY"
echo "  shared env: $SHARED_ENV"
