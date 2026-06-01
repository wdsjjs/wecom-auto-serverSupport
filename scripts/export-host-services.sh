#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${1:-$ROOT_DIR/deploy/migration}"
TS="$(date '+%Y%m%d%H%M%S')"
PG_VERSION="${PG_VERSION:-16}"
CSBOT_DB="${CSBOT_PG_DATABASE:-csbot_wecom}"
MEM0_DB="${MEM0_APP_DB_NAME:-mem0_app}"
MEM0_SRC="${MEM0_SERVER_DIR:-$HOME/Desktop/udaItem/mem0/server}"
MEM0_ENV_SRC="${MEM0_ENV_FILE:-$HOME/Library/Application Support/uda-mem0/mem0-api.env}"
MEM0_PLIST_SRC="${MEM0_PLIST_FILE:-$HOME/Library/LaunchAgents/com.uda.mem0-api.plist}"

load_homebrew_path() {
  if [[ -x "/opt/homebrew/bin/brew" ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  elif [[ -x "/usr/local/bin/brew" ]]; then
    eval "$(/usr/local/bin/brew shellenv)"
  fi
}

require_file() {
  if [[ ! -e "$1" ]]; then
    echo "ERROR: missing required path: $1" >&2
    exit 1
  fi
}

load_homebrew_path
mkdir -p "$OUT_DIR"

PG_DUMP="$(brew --prefix "postgresql@${PG_VERSION}")/bin/pg_dump"
if [[ ! -x "$PG_DUMP" ]]; then
  PG_DUMP="$(command -v pg_dump || true)"
fi
if [[ -z "$PG_DUMP" || ! -x "$PG_DUMP" ]]; then
  echo "ERROR: pg_dump not found. Install postgresql@${PG_VERSION} first." >&2
  exit 1
fi

require_file "$MEM0_SRC"
require_file "$MEM0_ENV_SRC"
require_file "$MEM0_PLIST_SRC"

echo "Export directory: $OUT_DIR"
echo "Exporting PostgreSQL database: $CSBOT_DB"
"$PG_DUMP" -Fc -d "$CSBOT_DB" -f "$OUT_DIR/${CSBOT_DB}.dump"

echo "Exporting MEM0 app database: $MEM0_DB"
"$PG_DUMP" -Fc -d "$MEM0_DB" -f "$OUT_DIR/${MEM0_DB}.dump"

echo "Packing MEM0 server directory: $MEM0_SRC"
tar \
  --exclude='.venv' \
  --exclude='__pycache__' \
  --exclude='*/__pycache__' \
  --exclude='logs' \
  -czf "$OUT_DIR/mem0-server.tar.gz" \
  -C "$(dirname "$MEM0_SRC")" \
  "$(basename "$MEM0_SRC")"

cp "$MEM0_ENV_SRC" "$OUT_DIR/mem0-api.env"
cp "$MEM0_PLIST_SRC" "$OUT_DIR/com.uda.mem0-api.plist.source"

cat > "$OUT_DIR/manifest.env" <<EOF
EXPORTED_AT='$TS'
SOURCE_HOME='$HOME'
PG_VERSION='$PG_VERSION'
CSBOT_PG_DATABASE='$CSBOT_DB'
CSBOT_PG_USER='csbot_app'
CSBOT_PG_PASSWORD='xvcV6THkqNgeIFG5FySxCFI1zxrsbupK'
MEM0_APP_DB_NAME='$MEM0_DB'
MEM0_ADMIN_API_KEY='k0A_hDPDQzcs6HaSv9k90xyRgqWyh2nQBTa_qfoblr8'
MEM0_PORT='8888'
EOF

cat > "$OUT_DIR/README.md" <<'EOF'
# UDA WeCom PG + MEM0 Migration Bundle

Copy this whole directory to the target Mac, then run from the project root:

```bash
./scripts/restore-host-services.sh /path/to/this/migration
```

The restore script installs/starts PostgreSQL via Homebrew if needed, restores
`csbot_wecom` and `mem0_app`, installs the MEM0 API LaunchAgent, and writes LAN
connection hints to `deploy/mac.shared.env`.
EOF

du -sh "$OUT_DIR"/*
echo
echo "Export complete: $OUT_DIR"
