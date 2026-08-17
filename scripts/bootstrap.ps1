$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Assert-NativeSuccess {
    param([string]$Step)
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE"
    }
}

$pythonExe = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
if (-not (Test-Path $pythonExe)) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        throw 'Python 3.11/3.12 was not found. Install it with: winget install Python.Python.3.12'
    }
    $pythonExe = $pythonCommand.Source
}

$nodeExe = Join-Path $env:ProgramFiles 'nodejs\node.exe'
$npmExe = Join-Path $env:ProgramFiles 'nodejs\npm.cmd'
if (-not (Test-Path $nodeExe) -or -not (Test-Path $npmExe)) {
    throw 'Node.js LTS was not found. Install it with: winget install OpenJS.NodeJS.LTS'
}
$env:PATH = (Split-Path -Parent $nodeExe) + ';' + $env:PATH

if (-not (Test-Path '.venv')) {
    & $pythonExe -m venv .venv
    Assert-NativeSuccess 'Creating the Python virtual environment'
}
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
Assert-NativeSuccess 'Upgrading pip'
& .\.venv\Scripts\python.exe -m pip install -e '.[dev]'
Assert-NativeSuccess 'Installing Python dependencies'
try {
    Push-Location frontend
    & $npmExe ci
    Assert-NativeSuccess 'Installing frontend dependencies'
    & $npmExe run build
    Assert-NativeSuccess 'Building the frontend'
} finally {
    Pop-Location
}
& .\.venv\Scripts\python.exe -m playwright install chromium
Assert-NativeSuccess 'Installing Playwright Chromium'
New-Item -ItemType Directory -Path data -Force | Out-Null
& .\.venv\Scripts\alembic.exe upgrade head
Assert-NativeSuccess 'Applying database migrations'
Write-Host 'Bootstrap complete. Start with: powershell -File scripts/start.ps1'
