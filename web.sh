#!/usr/bin/env bash
# Opens the browser version. Add --lan to reach it from a phone on the same wifi.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
[ -d .venv ] || bash scripts/setup.sh >/dev/null
VPY=".venv/bin/python"; [ -x "$VPY" ] || VPY=".venv/Scripts/python.exe"
exec "$VPY" scripts/lavabo_web.py "$@"
