#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONTROL="$ROOT_DIR/wecom-gui/scripts/wecom-control"

[[ -x "$CONTROL" ]] || { echo "ERROR: missing control script: $CONTROL" >&2; exit 1; }

"$CONTROL" start
