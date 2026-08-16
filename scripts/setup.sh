#!/usr/bin/env bash
#
# One-shot setup for macOS / Linux (and Git Bash on Windows).
#
#   bash scripts/setup.sh
#
# Creates the venv, installs everything, and seeds config files.
# Safe to re-run: existing config files and .env are never overwritten.

set -euo pipefail

# Always operate from the repo root, wherever this was invoked from.
cd "$(dirname "${BASH_SOURCE[0]}")/.."

BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; OFF=$'\033[0m'

step() { printf '\n%s==> %s%s\n' "$BOLD" "$1" "$OFF"; }
ok()   { printf '    %s%s%s\n' "$GREEN" "$1" "$OFF"; }
warn() { printf '    %s%s%s\n' "$YELLOW" "$1" "$OFF"; }
die()  { printf '\n%sERROR: %s%s\n\n' "$RED" "$1" "$OFF" >&2; exit 1; }

# ---------------------------------------------------------------- python

step "Looking for Python 3.11+"

PY=""
for candidate in python3.13 python3.12 python3.11 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 \
       && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
        PY="$candidate"
        break
    fi
done

if [ -z "$PY" ]; then
    found="$(command -v python3 >/dev/null 2>&1 && python3 --version 2>&1 || echo 'none found')"
    if [ "$(uname -s)" = "Darwin" ]; then
        die "No Python 3.11+ found (system python3: ${found}).
       macOS ships Python 3.9, which is too old. Install a newer one:

           brew install python@3.12

       then re-run: bash scripts/setup.sh"
    fi
    die "No Python 3.11+ found (python3: ${found}). Install Python 3.11 or newer, then re-run."
fi

ok "using $PY ($("$PY" --version 2>&1))"

# ------------------------------------------------------------------ venv

step "Creating the virtual environment"

if [ -d .venv ]; then
    ok ".venv already exists, reusing it"
else
    "$PY" -m venv .venv
    ok "created .venv"
fi

VPY=".venv/bin/python"
[ -x "$VPY" ] || VPY=".venv/Scripts/python.exe"      # Git Bash on Windows
[ -x "$VPY" ] || die "venv looks broken — delete .venv and re-run this script."

# --------------------------------------------------------------- install

step "Installing dependencies (this takes a minute)"

"$VPY" -m pip install --upgrade pip --quiet
"$VPY" -m pip install --quiet -r requirements.txt
"$VPY" -m pip install --quiet -e .
ok "dependencies installed"

# ---------------------------------------------------------------- config

step "Seeding config files (existing files are kept)"

seed() {
    if [ -f "$2" ]; then
        warn "kept    $2  (already exists, not overwritten)"
    else
        cp "$1" "$2"
        ok "created $2"
    fi
}

seed config/config.example.yaml config/config.yaml
# The SENKAHOMES schema, not the generic placeholder one: the senkahomes layout reads
# items/address/total_text/deposit_text, and seeding the example leaves an install that
# extracts the wrong fields and writes a nearly empty workbook.
seed config/schema.senkahomes.yaml config/schema.yaml
seed .env.example .env

mkdir -p data/inbox/zalo data/out
ok "data directories ready"

# ----------------------------------------------------------------- check

step "Running preflight"
set +e
"$VPY" -m lavabo.cli check
set -e

# ------------------------------------------------------------------ done

cat <<EOF

${BOLD}Setup complete.${OFF}

${BOLD}1. Activate the venv${OFF} (needed in every new terminal):

     source .venv/bin/activate

${BOLD}2. Edit config/config.yaml${OFF} — set your exact Zalo display name:

     zalo:
       own_names:
         - "Your Zalo Name"

   This is how the agent tells your messages from the customer's. Getting it
   wrong does not error, it silently mislabels every message.

${BOLD}3. Capture one conversation${OFF} to validate the format before doing all 50:

     python scripts/zalo_capture.py

   In Zalo: open a conversation, scroll to the TOP, then select-all and copy
   (Cmd+A/Cmd+C on macOS, Ctrl+A/Ctrl+C on Windows).
   Stop the script with Ctrl+C — always Ctrl, even on macOS.

${BOLD}4. Then run the pipeline${OFF}:

     lavabo ingest --source zalo
     lavabo extract --limit 1 --dry-run     # free, no API key needed
     lavabo extract --limit 1               # needs ANTHROPIC_API_KEY in .env
     lavabo load --out data/out/first.xlsx
     lavabo verify

Full walkthrough: docs/00-quickstart.md
EOF
