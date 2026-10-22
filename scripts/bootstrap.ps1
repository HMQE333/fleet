# Fleet: everything the loop needs, once. Run from the project folder in PowerShell.
#   .\scripts\bootstrap.ps1
# Needs Python 3.12 and Node 20+ on PATH. Keep the project path short (e.g. C:\fleet): paths over 260 characters break
# Python and git on Windows unless long paths are enabled.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path ".venv")) { python -m venv .venv }
& .\.venv\Scripts\python -m pip install --upgrade pip -q
& .\.venv\Scripts\python -m pip install -e . pytest pytest-asyncio ruff pgserver -q
& .\.venv\Scripts\python -c "import fleet, fleet.api, pgserver; print('python side ok')"

Push-Location web
if (-not (Test-Path "node_modules")) { npm install --no-audit --no-fund }
npm run build
Pop-Location

if (-not (Test-Path ".env")) { Copy-Item .env.example .env; Write-Host "Created .env from .env.example - put your keys in it." }
Write-Host "Done. Next: .\.venv\Scripts\python -m readiness --quick"
