param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8000,

    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
if (-not (Test-Path 'frontend/dist/index.html')) { throw 'Frontend is not built. Run scripts/bootstrap.ps1 first.' }

$appUrl = "http://127.0.0.1:$Port"
try {
    $health = Invoke-RestMethod -Uri "$appUrl/api/health" -TimeoutSec 2
    if ($health.status -eq 'ok') {
        Write-Host "Job Application Orchestrator is already running at $appUrl"
        if (-not $NoBrowser) {
            Start-Process $appUrl
        }
        return
    }
} catch {
    # No healthy application responded, so continue with the port check.
}

$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    throw "Port $Port is already used by process PID $($listener.OwningProcess). Stop it or choose another port with -Port."
}

Write-Host "Starting Job Application Orchestrator at $appUrl"
& .\.venv\Scripts\python.exe -m alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw 'Database migration failed' }
$browserJob = $null
if (-not $NoBrowser) {
    $browserJob = Start-Job -ScriptBlock {
        param($Url)
        for ($attempt = 0; $attempt -lt 60; $attempt++) {
            try {
                $health = Invoke-RestMethod -Uri "$Url/api/health" -TimeoutSec 1
                if ($health.status -eq 'ok') {
                    Start-Process $Url
                    return
                }
            } catch {
                # Wait until the server is ready.
            }
            Start-Sleep -Milliseconds 250
        }
    } -ArgumentList $appUrl
}

try {
    & .\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port $Port
    if ($LASTEXITCODE -ne 0) { throw "Application exited with code $LASTEXITCODE" }
} finally {
    if ($browserJob) {
        Stop-Job -Job $browserJob -ErrorAction SilentlyContinue
        Remove-Job -Job $browserJob -Force -ErrorAction SilentlyContinue
    }
}
