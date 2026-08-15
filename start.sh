#!/usr/bin/env bash
# Double-click me (macOS: start.command) or run: bash start.sh
#
# Sets everything up on first run, then opens the app. Safe to run any time.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if [ ! -d .venv ]; then
    echo "Đang cài đặt lần đầu, mất khoảng một phút..."
    bash scripts/setup.sh >/dev/null || { bash scripts/setup.sh; exit 1; }
fi

VPY=".venv/bin/python"
[ -x "$VPY" ] || VPY=".venv/Scripts/python.exe"

exec "$VPY" scripts/lavabo_app.py
