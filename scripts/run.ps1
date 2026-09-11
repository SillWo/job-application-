<#
    The single Windows development entry point.

    The script is intentionally compatible with Windows PowerShell 5.1.  It
    only installs missing tools/dependencies and keeps all user data in data/.
    Generated state used to avoid repeated work lives in ignored directories.
#>
[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8000,

    [switch]$NoBrowser,

    [switch]$ForceSetup,

    [switch]$ForceRebuild,

    [switch]$Stop
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $root

function Write-Step {
    param([string]$Message)
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Assert-NativeSuccess {
    param([string]$Step)
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE."
    }
}

function Refresh-ProcessPath {
    # Installers update the registry, but the current PowerShell process keeps
    # its old PATH.  Re-read both scopes so the newly installed tools are
    # usable without opening another terminal.
    $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $parts = @($env:PATH, $userPath, $machinePath) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    if ($parts.Count -gt 0) {
        $env:PATH = ($parts -join ';')
    }
}

function Get-CommandPath {
    param([string]$Name)
    $command = Get-Command $Name -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) {
        if ($command.PSObject.Properties['Source'] -and $command.Source) {
            return $command.Source
        }
        if ($command.PSObject.Properties['Path'] -and $command.Path) {
            return $command.Path
        }
    }
    return $null
}

function Invoke-NativeProbe {
    param(
        [string]$FilePath,
        [string[]]$Arguments
    )
    # Windows PowerShell 5.1 can turn a native program's stderr into a
    # terminating error when ErrorActionPreference is Stop. Probes are
    # expected to fail while detecting missing tools, so isolate that here.
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = @(& $FilePath @Arguments 2>$null)
        $exitCode = $LASTEXITCODE
        return [PSCustomObject]@{ ExitCode = $exitCode; Output = $output }
    } catch {
        return [PSCustomObject]@{ ExitCode = -1; Output = @() }
    } finally {
        $ErrorActionPreference = $previousPreference
    }
}

function Get-WingetPath {
    Refresh-ProcessPath
    $winget = Get-CommandPath 'winget.exe'
    if (-not $winget) {
        $winget = Get-CommandPath 'winget'
    }
    if (-not $winget) {
        $windowsAppsWinget = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\winget.exe'
        if (Test-Path -LiteralPath $windowsAppsWinget -PathType Leaf) {
            $winget = $windowsAppsWinget
        }
    }
    return $winget
}

function Ensure-Winget {
    $winget = Get-WingetPath
    if ($winget) { return $winget }

    Write-Step 'Preparing Windows Package Manager (WinGet)'
    # On a fresh Windows user profile App Installer can be present but not yet
    # registered. This is the registration command recommended by Microsoft.
    try {
        Add-AppxPackage -RegisterByFamilyName -MainPackage Microsoft.DesktopAppInstaller_8wekyb3d8bbwe -ErrorAction Stop
    } catch {
        Write-Host 'App Installer is not registered yet; trying the official WinGet repair module.' -ForegroundColor Yellow
    }
    $winget = Get-WingetPath
    if ($winget) { return $winget }

    # Windows Sandbox, LTSC images, and some newly provisioned machines do not
    # contain App Installer. Bootstrap it using Microsoft's documented module.
    $previousProgress = $ProgressPreference
    $gallery = Get-PSRepository -Name PSGallery -ErrorAction SilentlyContinue
    $restoreGalleryPolicy = $false
    try {
        $ProgressPreference = 'SilentlyContinue'
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        if ($gallery -and $gallery.InstallationPolicy -ne 'Trusted') {
            Set-PSRepository -Name PSGallery -InstallationPolicy Trusted
            $restoreGalleryPolicy = $true
        }
        Install-PackageProvider -Name NuGet -Force -Scope CurrentUser | Out-Null
        Install-Module -Name Microsoft.WinGet.Client -Force -Repository PSGallery -Scope CurrentUser -AllowClobber
        Import-Module Microsoft.WinGet.Client -Force
        Repair-WinGetPackageManager -Force -Latest | Out-Null
    } catch {
        throw "WinGet could not be installed automatically. Install Microsoft App Installer or run Windows Update, then try again. Details: $($_.Exception.Message)"
    } finally {
        if ($restoreGalleryPolicy) {
            Set-PSRepository -Name PSGallery -InstallationPolicy $gallery.InstallationPolicy -ErrorAction SilentlyContinue
        }
        $ProgressPreference = $previousProgress
    }

    Refresh-ProcessPath
    $winget = Get-WingetPath
    if (-not $winget) {
        throw 'WinGet installation completed, but winget.exe is not available. Sign out of Windows once, sign back in, and rerun the script.'
    }
    return $winget
}

function Install-WithWinget {
    param(
        [string]$PackageId,
        [string]$DisplayName
    )
    $winget = Ensure-Winget

    Write-Host "Installing $DisplayName through WinGet..." -ForegroundColor Yellow
    & $winget install --id $PackageId -e --source winget --accept-source-agreements --accept-package-agreements --silent --disable-interactivity
    Assert-NativeSuccess "Installing $DisplayName through WinGet"
    Refresh-ProcessPath
}

function Get-Python312 {
    $pyLauncher = Get-CommandPath 'py.exe'
    if (-not $pyLauncher) {
        $pyLauncher = Get-CommandPath 'py'
    }
    if ($pyLauncher) {
        $result = Invoke-NativeProbe $pyLauncher @('-3.12', '-c', 'import sys; print(sys.executable)')
        if ($result.ExitCode -eq 0 -and $result.Output.Count -gt 0) {
            $candidate = [string]$result.Output[$result.Output.Count - 1]
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                return (Resolve-Path -LiteralPath $candidate).Path
            }
        }
    }

    $knownPaths = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
        (Join-Path $env:ProgramFiles 'Python312\python.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Python312\python.exe')
    )
    foreach ($knownPath in $knownPaths) {
        if ($knownPath -and (Test-Path -LiteralPath $knownPath -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $knownPath).Path
        }
    }

    # A regular python command is accepted only when it is Python 3.11+;
    # Store's placeholder executable and old Python installations are ignored.
    $python = Get-CommandPath 'python.exe'
    if (-not $python) {
        $python = Get-CommandPath 'python'
    }
    if ($python) {
        $result = Invoke-NativeProbe $python @('-c', 'import sys; print(sys.version_info[0]); print(sys.version_info[1])')
        if ($result.ExitCode -eq 0 -and $result.Output.Count -ge 2) {
            $versionText = "{0}.{1}" -f $result.Output[0], $result.Output[1]
            try {
                $parsedVersion = [Version]$versionText
                if ($parsedVersion -ge [Version]'3.11') {
                    Write-Host "Using Python $versionText ($python). Python 3.12 is preferred." -ForegroundColor Yellow
                    return $python
                }
            } catch {
                # Continue to the automatic installation and diagnostics below.
            }
        }
    }
    return $null
}

function Get-NodeTools {
    $node = Get-CommandPath 'node.exe'
    if (-not $node) {
        $node = Get-CommandPath 'node'
    }
    $npm = Get-CommandPath 'npm.cmd'
    if (-not $npm) {
        $npm = Get-CommandPath 'npm'
    }
    if (-not $node -or -not $npm) {
        return $null
    }

    $result = Invoke-NativeProbe $node @('--version')
    if ($result.ExitCode -ne 0 -or $result.Output.Count -eq 0) {
        return $null
    }
    $versionText = [string]$result.Output[$result.Output.Count - 1]
    if ($versionText -notmatch '^v(\d+)') {
        return $null
    }
    $major = [int]$Matches[1]
    if ($major -lt 18) {
        Write-Host "Found Node.js $versionText, but Node.js 18+ (LTS) is required." -ForegroundColor Yellow
        return $null
    }
    return [PSCustomObject]@{ Node = $node; Npm = $npm; Version = $versionText }
}

function Get-DevelopmentTools {
    Write-Step 'Checking Python 3.12 and Node.js LTS'
    Refresh-ProcessPath

    $python = Get-Python312
    if (-not $python) {
        Install-WithWinget 'Python.Python.3.12' 'Python 3.12'
        $python = Get-Python312
        if (-not $python) {
            throw 'Python 3.12 was installed but is not available in this PowerShell session. Restart PowerShell and try again.'
        }
    }

    $nodeTools = Get-NodeTools
    if (-not $nodeTools) {
        Install-WithWinget 'OpenJS.NodeJS.LTS' 'Node.js LTS'
        $nodeTools = Get-NodeTools
        if (-not $nodeTools) {
            throw 'Node.js LTS was installed but node/npm are not available in this PowerShell session. Restart PowerShell and try again.'
        }
    }

    Write-Host "Python: $python"
    Write-Host "Node.js: $($nodeTools.Version)"
    return [PSCustomObject]@{ Python = $python; Node = $nodeTools.Node; Npm = $nodeTools.Npm }
}

function Get-FileFingerprint {
    param([string[]]$Paths)
    $entries = New-Object System.Collections.Generic.List[string]
    foreach ($path in $Paths) {
        if (-not (Test-Path -LiteralPath $path)) {
            continue
        }
        $item = Get-Item -LiteralPath $path
        $files = @()
        if ($item.PSIsContainer) {
            $files = Get-ChildItem -LiteralPath $item.FullName -Recurse -File | Where-Object {
                $_.FullName -notmatch '\\node_modules(\\|$)' -and
                $_.FullName -notmatch '\\dist(\\|$)' -and
                $_.FullName -notmatch '\\coverage(\\|$)' -and
                $_.Name -notmatch '\.tsbuildinfo$' -and
                $_.Name -notmatch '^\.env'
            }
        } else {
            $files = @($item)
        }
        foreach ($file in $files) {
            $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
            $relative = $file.FullName.Substring($root.Length).TrimStart('\', '/')
            [void]$entries.Add("$relative|$hash")
        }
    }
    $ordered = @($entries | Sort-Object)
    $text = $ordered -join "`n"
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($text)
        return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
}

function Read-Marker {
    param([string]$Path)
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        $value = Get-Content -LiteralPath $Path -Raw
        if ($null -ne $value) { return $value.Trim() }
    }
    return ''
}

function Write-Marker {
    param([string]$Path, [string]$Value)
    $parent = Split-Path -Parent $Path
    if ($parent) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    # UTF-8 keeps state paths valid when the checkout contains non-ASCII names.
    Set-Content -LiteralPath $Path -Value $Value -Encoding UTF8
}

function Ensure-PythonEnvironment {
    param([string]$BootstrapPython)
    $venvPython = Join-Path $root '.venv\Scripts\python.exe'
    $venvUsable = $false
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        $result = Invoke-NativeProbe $venvPython @('-c', 'import sys; print(sys.version_info[0]); print(sys.version_info[1]); print(sys.executable)')
        $venvUsable = ($result.ExitCode -eq 0 -and $result.Output.Count -ge 3 -and [int]$result.Output[0] -eq 3 -and [int]$result.Output[1] -ge 11)
    }
    if (-not $venvUsable -and (Test-Path -LiteralPath (Join-Path $root '.venv'))) {
        $backupRoot = Join-Path $root 'tmp\run\venv-backups'
        New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
        $backup = Join-Path $backupRoot ('venv-broken-' + (Get-Date -Format 'yyyyMMdd-HHmmssfff'))
        Move-Item -LiteralPath (Join-Path $root '.venv') -Destination $backup
        Write-Host "Moved unusable .venv to $backup" -ForegroundColor Yellow
    }
    if (-not $venvUsable) {
        Write-Step 'Creating Python virtual environment'
        & $BootstrapPython -m venv (Join-Path $root '.venv')
        Assert-NativeSuccess 'Creating Python virtual environment'
    }

    $dependencyFingerprint = Get-FileFingerprint @((Join-Path $root 'pyproject.toml'))
    $marker = Join-Path $root '.venv\.job-application-python-dependencies.sha256'
    if ($ForceSetup -or (Read-Marker $marker) -ne $dependencyFingerprint) {
        Write-Step 'Installing Python dependencies'
        & $venvPython -m pip install --upgrade pip
        Assert-NativeSuccess 'Updating pip'
        & $venvPython -m pip install -e '.[dev]'
        Assert-NativeSuccess 'Installing Python dependencies'
        Write-Marker $marker $dependencyFingerprint
    } else {
        Write-Host 'Python dependencies already match pyproject.toml.'
    }
    return [PSCustomObject]@{ Python = $venvPython; Fingerprint = $dependencyFingerprint }
}

function Ensure-FrontendDependencies {
    param([string]$Npm)
    $frontend = Join-Path $root 'frontend'
    $fingerprint = Get-FileFingerprint @(
        (Join-Path $frontend 'package.json'),
        (Join-Path $frontend 'package-lock.json')
    )
    $nodeModules = Join-Path $frontend 'node_modules'
    $marker = Join-Path $nodeModules '.job-application-node-dependencies.sha256'
    if ($ForceSetup -or -not (Test-Path -LiteralPath $nodeModules -PathType Container) -or (Read-Marker $marker) -ne $fingerprint) {
        Write-Step 'Installing frontend dependencies'
        Push-Location -LiteralPath $frontend
        try {
            if (Test-Path -LiteralPath 'package-lock.json' -PathType Leaf) {
                & $Npm ci
            } else {
                & $Npm install
            }
            Assert-NativeSuccess 'Installing frontend dependencies'
        } finally {
            Pop-Location
        }
        Write-Marker $marker $fingerprint
    } else {
        Write-Host 'Frontend dependencies already match package-lock.json.'
    }
}

function Ensure-PlaywrightChromium {
    param(
        [string]$VenvPython,
        [string]$DependencyFingerprint
    )
    $marker = Join-Path $root '.venv\.job-application-playwright-chromium.sha256'
    $result = Invoke-NativeProbe $VenvPython @('-c', 'from playwright.sync_api import sync_playwright; p=sync_playwright().start(); print(p.chromium.executable_path); p.stop()')
    $browserExists = $false
    if ($result.ExitCode -eq 0 -and $result.Output.Count -gt 0) {
        $browserPath = [string]$result.Output[$result.Output.Count - 1]
        $browserExists = Test-Path -LiteralPath $browserPath -PathType Leaf
    }
    if ($ForceSetup -or (Read-Marker $marker) -ne $DependencyFingerprint -or -not $browserExists) {
        Write-Step 'Installing Playwright Chromium'
        & $VenvPython -m playwright install chromium
        Assert-NativeSuccess 'Installing Playwright Chromium'
        Write-Marker $marker $DependencyFingerprint
    } else {
        Write-Host 'Playwright Chromium is already installed.'
    }
}

function Get-ListeningProcessId {
    param([int]$ListenPort)
    try {
        $connection = Get-NetTCPConnection -LocalPort $ListenPort -State Listen -ErrorAction Stop | Select-Object -First 1
        if ($connection) {
            return [int]$connection.OwningProcess
        }
    } catch {
        # Fall back to netstat on older Windows builds or restricted sessions.
    }
    $line = netstat.exe -ano -p tcp 2>$null | Select-String (':{0}\s+.*LISTENING\s+(\d+)$' -f $ListenPort) | Select-Object -First 1
    if ($line -and $line.Matches.Count -gt 0) {
        return [int]$line.Matches[0].Groups[1].Value
    }
    return $null
}

function Test-HealthyApplication {
    param([string]$Url)
    try {
        $health = Invoke-RestMethod -Uri "$Url/api/health" -Method Get -TimeoutSec 2
        return ($health.status -eq 'ok')
    } catch {
        return $false
    }
}

function Get-BackendStatePath {
    param([int]$ListenPort)
    return (Join-Path $root ("tmp\run\backend-$ListenPort.state"))
}

function Get-ProcessDetails {
    param([int]$ProcessId)
    try {
        return Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $ProcessId) -ErrorAction Stop | Select-Object -First 1
    } catch {
        try {
            return Get-WmiObject Win32_Process -Filter ("ProcessId = {0}" -f $ProcessId) -ErrorAction Stop | Select-Object -First 1
        } catch {
            return $null
        }
    }
}

function Normalize-FullPath {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return '' }
    try { return ([IO.Path]::GetFullPath($Path)).TrimEnd('\') } catch { return $Path.TrimEnd('\') }
}

function Test-ManagedBackendProcess {
    param([int]$ListenPort, [int]$ProcessId)
    $expectedPython = Normalize-FullPath (Join-Path $root '.venv\Scripts\python.exe')
    $details = Get-ProcessDetails $ProcessId
    if (-not $details -or [string]::IsNullOrWhiteSpace($details.CommandLine)) { return $false }
    # A Windows venv launches the base interpreter, so Win32_Process.ExecutablePath
    # can point outside .venv. The exact venv path is still retained in CommandLine.
    if ($details.CommandLine.IndexOf($expectedPython, [StringComparison]::OrdinalIgnoreCase) -lt 0) { return $false }
    if ($details.CommandLine -notmatch 'backend\.main:app' -or $details.CommandLine -notmatch ("--port\s+{0}(\s|$)" -f $ListenPort)) { return $false }

    # The state file records processes started by run.ps1. It is advisory: the
    # Windows venv launcher can create a child base-python process that owns the
    # socket, and older launcher versions may therefore contain the parent PID.
    # The exact checkout-local command line above remains the ownership check.
    $statePath = Get-BackendStatePath $ListenPort
    if (Test-Path -LiteralPath $statePath -PathType Leaf) {
        try {
            $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
            if ((Normalize-FullPath $state.root) -ne (Normalize-FullPath $root)) { return $false }
            if ((Normalize-FullPath $state.python) -ne $expectedPython) { return $false }
        } catch {
            Write-Host "Ignoring an unreadable backend state file: $statePath" -ForegroundColor Yellow
        }
    }
    return $true
}

function Stop-ManagedBackend {
    param([int]$ListenPort, [int]$ProcessId)
    if (-not (Test-ManagedBackendProcess $ListenPort $ProcessId)) {
        throw 'The process on the application port could not be verified as belonging to this checkout; it was not stopped.'
    }
    Stop-Process -Id $ProcessId -ErrorAction Stop
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        if (-not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 250
    }
    if (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) {
        Stop-Process -Id $ProcessId -Force -ErrorAction Stop
    }
    Remove-Item -LiteralPath (Get-BackendStatePath $ListenPort) -Force -ErrorAction SilentlyContinue
    Write-Host "Stopped the previous backend owned by this checkout (PID $ProcessId)." -ForegroundColor Yellow
}

function Handle-ExistingApplication {
    param([int]$ListenPort)
    $url = "http://127.0.0.1:$ListenPort"
    $owner = Get-ListeningProcessId $ListenPort
    if (-not $owner) { return }
    if ($owner -and (Test-ManagedBackendProcess $ListenPort $owner)) {
        Stop-ManagedBackend $ListenPort $owner
        return
    }
    $description = if (Test-HealthyApplication $url) { 'a healthy web application' } else { 'another process' }
    throw "Port $ListenPort is already used by $description (PID $owner). It was not stopped because it does not belong to this checkout; use -Port to choose another port."
}

function Start-Application {
    param(
        [string]$VenvPython,
        [int]$ListenPort
    )
    $url = "http://127.0.0.1:$ListenPort"
    Handle-ExistingApplication $ListenPort

    $owner = Get-ListeningProcessId $ListenPort
    if ($owner) {
        throw "Port $ListenPort is already used by PID $owner. The process was not stopped; use -Port to choose another port."
    }

    Write-Step "Starting backend on port $ListenPort"
    $arguments = @('-m', 'uvicorn', 'backend.main:app', '--host', '127.0.0.1', '--port', [string]$ListenPort)
    # Keep Uvicorn attached to this console. This makes the normal development
    # run observable and lets Ctrl+C interrupt the foreground wait. Do not use
    # RedirectStandardOutput/RedirectStandardError here: with -NoNewWindow the
    # child writes directly to the same PowerShell console.
    $backend = Start-Process -FilePath $VenvPython -ArgumentList $arguments -WorkingDirectory $root -NoNewWindow -PassThru
    $statePath = Get-BackendStatePath $ListenPort
    try {
        $ready = $false
        for ($attempt = 0; $attempt -lt 120; $attempt++) {
            if ($backend.HasExited) {
                break
            }
            if (Test-HealthyApplication $url) {
                $ready = $true
                break
            }
            Start-Sleep -Milliseconds 250
        }
        if (-not $ready) {
            if ($backend.HasExited) {
                throw 'Backend exited before becoming ready. Review the Uvicorn output above.'
            }
            throw 'Backend did not respond within 30 seconds. Review the Uvicorn output above.'
        }

        $listenerProcessId = Get-ListeningProcessId $ListenPort
        if (-not $listenerProcessId) {
            $listenerProcessId = $backend.Id
        }
        Write-Host "Application started at $url (PID $listenerProcessId). Uvicorn output is shown below." -ForegroundColor Green
        Write-Host 'Press Ctrl+C to stop the application.' -ForegroundColor Yellow
        $state = [PSCustomObject]@{
            pid = $listenerProcessId
            root = Normalize-FullPath $root
            python = Normalize-FullPath $VenvPython
            port = $ListenPort
        }
        Write-Marker $statePath ($state | ConvertTo-Json -Compress)
        if (-not $NoBrowser) {
            # Opening the URL is deliberately fire-and-forget; the server is
            # already healthy and remains owned by this foreground process.
            try {
                Start-Process -FilePath $url | Out-Null
            } catch {
                Write-Warning "The application is ready, but Windows could not open the browser automatically: $($_.Exception.Message)"
            }
        }

        # Keep this PowerShell window attached to the backend until it exits or
        # the user presses Ctrl+C. The finally block below owns cleanup in both
        # cases.
        Wait-Process -Id $backend.Id
    } finally {
        # Ctrl+C interrupts Wait-Process, so cleanup must live in finally.
        # Prefer the listener PID (the venv launcher may hand the socket to a
        # child), and verify ownership before stopping anything. If no listener
        # exists, stop the exact process started above only when its command
        # line still identifies this checkout.
        $listenerProcessId = Get-ListeningProcessId $ListenPort
        if ($listenerProcessId -and (Test-ManagedBackendProcess $ListenPort $listenerProcessId)) {
            try {
                Stop-ManagedBackend $ListenPort $listenerProcessId
            } catch {
                Write-Warning "Could not stop the managed backend automatically: $($_.Exception.Message)"
            }
        } elseif ($backend -and (Get-Process -Id $backend.Id -ErrorAction SilentlyContinue)) {
            $details = Get-ProcessDetails $backend.Id
            $expectedPython = Normalize-FullPath (Join-Path $root '.venv\Scripts\python.exe')
            if ($details -and $details.CommandLine -and
                $details.CommandLine.IndexOf($expectedPython, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
                $details.CommandLine -match 'backend\.main:app' -and
                $details.CommandLine -match ("--port\s+{0}(\s|$)" -f $ListenPort)) {
                Stop-Process -Id $backend.Id -Force -ErrorAction SilentlyContinue
            } else {
                Write-Warning "The original backend process could not be verified; it was left running (PID $($backend.Id))."
            }
        }
        Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
    }
}

try {
    if ($Stop) {
        $owner = Get-ListeningProcessId $Port
        if (-not $owner) {
            Write-Host "No application is running on port $Port."
            return
        }
        if (-not (Test-ManagedBackendProcess $Port $owner)) {
            throw "Port $Port is used by PID $owner, but that process does not belong to this checkout and was not stopped."
        }
        Stop-ManagedBackend $Port $owner
        return
    }

    # Do not migrate a database while a confirmed copy of this checkout is running.
    # An unknown process is never stopped automatically.
    Handle-ExistingApplication $Port

    $tools = Get-DevelopmentTools
    $environment = Ensure-PythonEnvironment $tools.Python
    Ensure-FrontendDependencies $tools.Npm
    Ensure-PlaywrightChromium $environment.Python $environment.Fingerprint

    $frontendFingerprint = Get-FileFingerprint @(
        (Join-Path $root 'frontend\src'),
        (Join-Path $root 'frontend\public'),
        (Join-Path $root 'frontend\index.html'),
        (Join-Path $root 'frontend\package.json'),
        (Join-Path $root 'frontend\package-lock.json'),
        (Join-Path $root 'frontend\tsconfig.json'),
        (Join-Path $root 'frontend\tsconfig.app.json'),
        (Join-Path $root 'frontend\tsconfig.node.json'),
        (Join-Path $root 'frontend\vite.config.ts'),
        (Join-Path $root 'frontend\vite.config.js')
    )
    $buildMarker = Join-Path $root 'frontend\dist\.job-application-build.sha256'
    if ($ForceRebuild -or -not (Test-Path -LiteralPath (Join-Path $root 'frontend\dist\index.html') -PathType Leaf) -or (Read-Marker $buildMarker) -ne $frontendFingerprint) {
        Write-Step 'Building frontend'
        Push-Location -LiteralPath (Join-Path $root 'frontend')
        try {
            & $tools.Npm run build
            Assert-NativeSuccess 'Building frontend'
        } finally {
            Pop-Location
        }
        Write-Marker $buildMarker $frontendFingerprint
    } else {
        Write-Host 'Frontend is already built and matches the source files.'
    }

    Handle-ExistingApplication $Port

    Write-Step 'Applying database migrations'
    New-Item -ItemType Directory -Path (Join-Path $root 'data') -Force | Out-Null
    & $environment.Python -m alembic upgrade head
    Assert-NativeSuccess 'Applying database migrations'

    Start-Application $environment.Python $Port
} catch {
    Write-Error $_
    exit 1
}
