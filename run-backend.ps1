# Convenience launcher for the FastAPI backend (Windows / PowerShell).
# Builds the trial index at startup and serves on http://127.0.0.1:8000.
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path $here "backend\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Error "venv not found. Run: py -3.11 -m venv backend\.venv; backend\.venv\Scripts\pip install -r backend\requirements.txt"
}
if (-not (Test-Path (Join-Path $here ".env"))) {
    Write-Warning "No .env found — copy .env.example to .env and set FMT_SECRET_KEY + FMT_ADMIN_PASSWORD."
}
Push-Location (Join-Path $here "backend")
try { & $py -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000 }
finally { Pop-Location }
