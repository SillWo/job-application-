$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$backend = Start-Process -FilePath '.\.venv\Scripts\python.exe' -ArgumentList '-m','uvicorn','backend.main:app','--host','127.0.0.1','--port','8000','--reload' -PassThru -WindowStyle Hidden
try {
    Push-Location frontend
    npm.cmd run dev
    Pop-Location
} finally {
    if (-not $backend.HasExited) { Stop-Process -Id $backend.Id }
}
