# Setup for Windows (PowerShell):  .\setup.ps1
# If scripts are blocked:  Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"

$py = Get-Command py -ErrorAction SilentlyContinue
if ($py) { $python = "py"; $pyArgs = @("-3") } else {
  $python = "python"; $pyArgs = @()
  if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "Python 3.10+ is not installed. Download it from https://www.python.org/downloads/ (tick 'Add to PATH')." -ForegroundColor Red
    exit 1
  }
}

Write-Host "> Creating virtual environment (.venv)..."
& $python @pyArgs -m venv .venv
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

Write-Host "> Installing libraries..."
& $venvPy -m pip install --upgrade pip -q
& $venvPy -m pip install -r requirements.txt -q

if (-not (Test-Path ".env")) {
  Copy-Item ".env.example" ".env"
  Write-Host "> Created .env - add ANTHROPIC_API_KEY to enable reading photos"
}

Write-Host "> Creating demo data..."
& $venvPy -m app demo

Write-Host "> Running tests..."
& $venvPy -m unittest discover tests

Write-Host ""
Write-Host "Done. To start the app:" -ForegroundColor Green
Write-Host "    .venv\Scripts\python -m app"
Write-Host "Then open http://localhost:8000   (demo@example.com / demo1234)"
