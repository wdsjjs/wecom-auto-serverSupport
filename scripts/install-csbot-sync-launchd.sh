#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CSBOT_DIR="$ROOT_DIR/codex-csbot-wecom"
VENV_PY="$ROOT_DIR/.venv/bin/python"
PLIST="$HOME/Library/LaunchAgents/cn.uda.csbot-sync.plist"
LOG_DIR="$CSBOT_DIR/.codex-run"
RUNNER="$LOG_DIR/run-sync.sh"
HOUR="${CSBOT_SYNC_HOUR:-3}"
MINUTE="${CSBOT_SYNC_MINUTE:-10}"

mkdir -p "$LOG_DIR" "$HOME/Library/LaunchAgents"

if [[ ! -x "$VENV_PY" ]]; then
  echo "ERROR: missing venv Python: $VENV_PY" >&2
  echo "Run ./scripts/install-deps.sh first." >&2
  exit 1
fi

cat > "$RUNNER" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "$CSBOT_DIR"
if [[ -f "$CSBOT_DIR/.env" ]]; then
  set -a
  source "$CSBOT_DIR/.env"
  set +a
fi
exec "$VENV_PY" -m csbot sync all --progress
EOF
chmod 700 "$RUNNER"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>cn.uda.csbot-sync</string>
  <key>ProgramArguments</key>
  <array>
    <string>$RUNNER</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>$HOUR</integer>
    <key>Minute</key>
    <integer>$MINUTE</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>$LOG_DIR/sync.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/sync.err.log</string>
  <key>WorkingDirectory</key>
  <string>$CSBOT_DIR</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST" >/dev/null 2>&1 || true
launchctl load "$PLIST"

echo "Installed launchd sync job."
echo "  plist: $PLIST"
echo "  time: ${HOUR}:${MINUTE}"
echo "  log: $LOG_DIR/sync.log"
echo "  err: $LOG_DIR/sync.err.log"
