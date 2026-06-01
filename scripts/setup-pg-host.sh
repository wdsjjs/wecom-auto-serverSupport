#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PG_VERSION="${PG_VERSION:-16}"
PG_SERVICE="postgresql@${PG_VERSION}"
PG_DATABASE="${CSBOT_PG_DATABASE:-csbot_wecom}"
PG_USER="${CSBOT_PG_USER:-csbot_app}"
PG_PASSWORD="${CSBOT_PG_PASSWORD:-}"
LAN_CIDR="${CSBOT_PG_LAN_CIDR:-}"
SHARED_ENV="$ROOT_DIR/deploy/mac.shared.env"

load_homebrew_path() {
  if [[ -x "/opt/homebrew/bin/brew" ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  elif [[ -x "/usr/local/bin/brew" ]]; then
    eval "$(/usr/local/bin/brew shellenv)"
  fi
}

detect_lan_cidr() {
  if [[ -n "$LAN_CIDR" ]]; then
    printf "%s\n" "$LAN_CIDR"
    return 0
  fi
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
    brew --prefix "$PG_SERVICE" >/dev/null
    local prefix
    prefix="$(brew --prefix)"
    printf "%s/var/%s\n" "$prefix" "$PG_SERVICE"
  fi
}

append_managed_pg_conf() {
  local data_dir="$1"
  local cidr="$2"
  local pg_conf="$data_dir/postgresql.conf"
  local hba_conf="$data_dir/pg_hba.conf"
  local ts
  ts="$(date '+%Y%m%d%H%M%S')"
  cp "$pg_conf" "$pg_conf.bak.$ts"
  cp "$hba_conf" "$hba_conf.bak.$ts"

  if ! grep -q "UDA CSBOT managed listen_addresses" "$pg_conf"; then
    {
      echo ""
      echo "# UDA CSBOT managed listen_addresses"
      echo "listen_addresses = '*'"
    } >> "$pg_conf"
  fi

  if ! grep -q "UDA CSBOT managed LAN access" "$hba_conf"; then
    {
      echo ""
      echo "# UDA CSBOT managed LAN access"
      echo "host    $PG_DATABASE    $PG_USER    $cidr    scram-sha-256"
    } >> "$hba_conf"
  fi
}

sql_literal() {
  local value="$1"
  value="${value//\'/\'\'}"
  printf "'%s'" "$value"
}

sql_ident_literal() {
  local value="$1"
  value="${value//\"/\"\"}"
  printf '"%s"' "$value"
}

write_shared_env_hint() {
  local dsn="$1"
  mkdir -p "$(dirname "$SHARED_ENV")"
  touch "$SHARED_ENV"
  chmod 600 "$SHARED_ENV"
  if ! grep -Eq "^[[:space:]]*CSBOT_PG_DSN=" "$SHARED_ENV"; then
    printf "CSBOT_PG_DSN='%s'\n" "$dsn" >> "$SHARED_ENV"
  fi
}

load_homebrew_path
if ! command -v brew >/dev/null 2>&1; then
  echo "ERROR: Homebrew is required. Run ./scripts/install-deps.sh first." >&2
  exit 1
fi

brew list "$PG_SERVICE" >/dev/null 2>&1 || brew install "$PG_SERVICE"
brew services start "$PG_SERVICE"

if [[ -z "$PG_PASSWORD" ]]; then
  if command -v openssl >/dev/null 2>&1; then
    PG_PASSWORD="$(openssl rand -base64 24 | tr -d '\n')"
  else
    PG_PASSWORD="$(uuidgen | tr '[:upper:]' '[:lower:]')"
  fi
fi

PSQL="$(brew --prefix "$PG_SERVICE")/bin/psql"

for _ in {1..20}; do
  if "$PSQL" -d postgres -Atc "SELECT 1" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! "$PSQL" -d postgres -Atc "SELECT 1" >/dev/null 2>&1; then
  echo "ERROR: PostgreSQL did not become ready." >&2
  exit 1
fi

PG_USER_SQL="$(sql_literal "$PG_USER")"
PG_USER_IDENT="$(sql_ident_literal "$PG_USER")"
PG_DB_SQL="$(sql_literal "$PG_DATABASE")"
PG_DB_IDENT="$(sql_ident_literal "$PG_DATABASE")"
PG_PASSWORD_SQL="$(sql_literal "$PG_PASSWORD")"

if ! "$PSQL" -d postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname = $PG_USER_SQL" | grep -q 1; then
  "$PSQL" -d postgres -v ON_ERROR_STOP=1 -c "CREATE ROLE $PG_USER_IDENT LOGIN PASSWORD $PG_PASSWORD_SQL;"
else
  "$PSQL" -d postgres -v ON_ERROR_STOP=1 -c "ALTER ROLE $PG_USER_IDENT WITH LOGIN PASSWORD $PG_PASSWORD_SQL;"
fi

if ! "$PSQL" -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = $PG_DB_SQL" | grep -q 1; then
  "$PSQL" -d postgres -v ON_ERROR_STOP=1 -c "CREATE DATABASE $PG_DB_IDENT OWNER $PG_USER_IDENT;"
fi
"$PSQL" -d "$PG_DATABASE" -v ON_ERROR_STOP=1 -c "GRANT ALL PRIVILEGES ON DATABASE $PG_DB_IDENT TO $PG_USER_IDENT;"
"$PSQL" -d "$PG_DATABASE" -v ON_ERROR_STOP=1 -c "GRANT ALL ON SCHEMA public TO $PG_USER_IDENT;"

DATA_DIR="$(postgres_var_dir)"
CIDR="$(detect_lan_cidr)"
append_managed_pg_conf "$DATA_DIR" "$CIDR"
brew services restart "$PG_SERVICE"

HOST_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || hostname)"
DSN="postgresql://${PG_USER}:${PG_PASSWORD}@${HOST_IP}:5432/${PG_DATABASE}"
write_shared_env_hint "$DSN"

echo "PostgreSQL host ready."
echo "  database: $PG_DATABASE"
echo "  user: $PG_USER"
echo "  LAN CIDR: $CIDR"
echo "  DSN written to: $SHARED_ENV"
echo "  password: <set>"
