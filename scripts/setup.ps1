# One-shot setup for Windows PowerShell.
#
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
#
# Creates the venv, installs everything, and seeds config files.
# Safe to re-run: existing config files and .env are never overwritten.

$ErrorActionPreference = "Stop"

# Always operate from the repo root, wherever this was invoked from.
Set-Location (Join-Path $PSScriptRoot "..")

function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "    $m"   -ForegroundColor Green }
function Warn($m) { Write-Host "    $m"   -ForegroundColor Yellow }
function Die($m)  { Write-Host "`nERROR: $m`n" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------- python

Step "Looking for Python 3.11+"

$py = $null
foreach ($cand in @("python3.13", "python3.12", "python3.11", "python", "python3")) {
    if (Get-Command $cand -ErrorAction SilentlyContinue) {
        & $cand -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { $py = $cand; break }
    }
}

# The Windows launcher can reach versions not on PATH under their own name.
if (-not $py -and (Get-Command "py" -ErrorAction SilentlyContinue)) {
    foreach ($v in @("3.13", "3.12", "3.11")) {
        & py "-$v" -c "import sys; sys.exit(0)" 2>$null
        if ($LASTEXITCODE -eq 0) { $py = "py"; $pyArgs = "-$v"; break }
    }
}

if (-not $py) {
    Die @"
No Python 3.11+ found.
       Install it from https://www.python.org/downloads/ (tick "Add python.exe
       to PATH" during setup), then re-run this script.
"@
}

$pyCmd = if ($pyArgs) { "$py $pyArgs" } else { $py }
Ok "using $pyCmd"

# ------------------------------------------------------------------ venv

Step "Creating the virtual environment"

if (Test-Path ".venv") {
    Ok ".venv already exists, reusing it"
} else {
    if ($pyArgs) { & $py $pyArgs -m venv .venv } else { & $py -m venv .venv }
    Ok "created .venv"
}

$vpy = ".venv\Scripts\python.exe"
if (-not (Test-Path $vpy)) { Die "venv looks broken - delete .venv and re-run this script." }

# --------------------------------------------------------------- install

Step "Installing dependencies (this takes a minute)"

& $vpy -m pip install --upgrade pip --quiet
& $vpy -m pip install --quiet -r requirements.txt
& $vpy -m pip install --quiet -e .
Ok "dependencies installed"

# ---------------------------------------------------------------- config

Step "Seeding config files (existing files are kept)"

function Seed($src, $dst) {
    if (Test-Path $dst) {
        Warn "kept    $dst  (already exists, not overwritten)"
    } else {
        Copy-Item $src $dst
        Ok "created $dst"
    }
}

Seed "config\config.example.yaml" "config\config.yaml"
# The SENKAHOMES schema, not the generic placeholder one -- see setup.sh.
Seed "config\schema.senkahomes.yaml" "config\schema.yaml"
Seed ".env.example" ".env"

New-Item -ItemType Directory -Force -Path "data\inbox\zalo", "data\out" | Out-Null
Ok "data directories ready"

# ----------------------------------------------------------------- check

Step "Running preflight"
& $vpy -m lavabo.cli check

# ------------------------------------------------------------------ done

Write-Host @"

Setup complete.

1. Activate the venv (needed in every new terminal):

     .venv\Scripts\activate

   If PowerShell blocks it, run once:
     Set-ExecutionPolicy -Scope CurrentUser RemoteSigned

2. Edit config\config.yaml - set your exact Zalo display name:

     zalo:
       own_names:
         - "Your Zalo Name"

   This is how the agent tells your messages from the customer's. Getting it
   wrong does not error, it silently mislabels every message.

3. Capture one conversation to validate the format before doing all 50:

     python scripts\zalo_capture.py

   In Zalo: open a conversation, scroll to the TOP, then Ctrl+A, Ctrl+C.
   Stop the script with Ctrl+C.

4. Then run the pipeline:

     lavabo ingest --source zalo
     lavabo extract --limit 1 --dry-run     # free, no API key needed
     lavabo extract --limit 1               # needs ANTHROPIC_API_KEY in .env
     lavabo load --out data\out\first.xlsx
     lavabo verify

Full walkthrough: docs\00-quickstart.md
"@ -ForegroundColor White
