"""Isolated Windows tests for run.ps1's frontend dependency detection."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
RUN_PS1 = Path(__file__).resolve().parents[1] / "run.ps1"


pytestmark = pytest.mark.skipif(os.name != "nt" or POWERSHELL is None, reason="requires Windows PowerShell")


def run_case(*, marker: bool = True, force: bool = False, missing: str | None = None,
             probe_fails: bool = False, npm_fails: bool = False, repair: bool = True,
             has_lock: bool = True) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory(prefix="frontend-toolchain-") as tmp:
        fixture = Path(tmp) / "project"
        frontend = fixture / "frontend"
        modules = frontend / "node_modules"
        paths = {
            "shim_tsc": modules / ".bin" / "tsc.cmd",
            "shim_vite": modules / ".bin" / "vite.cmd",
            "entry_tsc": modules / "typescript" / "bin" / "tsc",
            "entry_vite": modules / "vite" / "bin" / "vite.js",
        }
        frontend.mkdir(parents=True)
        modules.mkdir()
        (frontend / "package.json").write_text("{}", encoding="utf-8")
        if has_lock:
            (frontend / "package-lock.json").write_text("{}", encoding="utf-8")
        for path in paths.values():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("shim", encoding="utf-8")
        if missing:
            paths[missing].unlink()
        marker_path = modules / ".job-application-node-dependencies.sha256"
        if marker:
            marker_path.write_text("fingerprint", encoding="utf-8")

        script = r'''
$ErrorActionPreference = 'Stop'
$root = $env:FIXTURE_ROOT
$ForceSetup = [bool]::Parse($env:FORCE_SETUP)
$script:ProbeFail = [bool]::Parse($env:PROBE_FAIL)
$script:NpmFail = [bool]::Parse($env:NPM_FAIL)
$script:Repair = [bool]::Parse($env:REPAIR_TOOLCHAIN)
$script:HasLock = [bool]::Parse($env:HAS_LOCK)
$script:NpmCalls = @()
$script:ProbeCalls = @()
$script:MarkerWrites = 0

$source = [System.IO.File]::ReadAllText($env:RUN_PS1)
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($source, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -gt 0) { throw "Could not parse run.ps1: $($parseErrors[0].Message)" }
$functionsToLoad = @('Test-FrontendToolchain', 'Ensure-FrontendDependencies')
foreach ($name in $functionsToLoad) {
    $definition = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $true)
    if (-not $definition) { throw "Missing function $name" }
    . ([scriptblock]::Create($definition.Extent.Text))
}

function Get-FileFingerprint { return 'fingerprint' }
function Read-Marker([string]$Path) { if (Test-Path -LiteralPath $Path -PathType Leaf) { return [System.IO.File]::ReadAllText($Path) }; return $null }
function Write-Marker([string]$Path, [string]$Value) { $script:MarkerWrites++; [System.IO.File]::WriteAllText($Path, $Value) }
function Write-Step([string]$Message) {}
function Assert-NativeSuccess([string]$Step) { if ($script:NpmFail) { throw "$Step failed with exit code 17." } }
function Invoke-NativeProbe([string]$FilePath, [string[]]$Arguments) {
    $script:ProbeCalls += [pscustomobject]@{ Path = $FilePath; Arguments = ($Arguments -join ' ') }
    if ($script:ProbeFail) { return [pscustomobject]@{ ExitCode = 1; Output = @() } }
    if (-not [System.IO.Path]::IsPathRooted($FilePath)) { throw "Probe did not use a full path: $FilePath" }
    if (($Arguments -join ' ') -ne '--version') { throw "Unexpected probe args: $Arguments" }
    return [pscustomobject]@{ ExitCode = 0; Output = @('1.0.0') }
}
function Mock-Npm {
    $script:NpmCalls += ,@($args)
    if ($script:NpmFail) { return }
    if ($script:Repair) {
        $nodeModules = Join-Path $root 'frontend\node_modules'
        foreach ($relative in @('.bin\tsc.cmd', '.bin\vite.cmd', 'typescript\bin\tsc', 'vite\bin\vite.js')) {
            $path = Join-Path $nodeModules $relative
            $parent = Split-Path -Parent $path
            if (-not (Test-Path -LiteralPath $parent -PathType Container)) { [void](New-Item -ItemType Directory -Path $parent -Force) }
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { [System.IO.File]::WriteAllText($path, 'shim') }
        }
        $script:ProbeFail = $false
    }
}

try { Ensure-FrontendDependencies 'Mock-Npm'; $outcome = 'success' }
catch { $outcome = 'error'; $errorText = $_.Exception.Message }
$frontend = Join-Path $root 'frontend'
$nodeModules = Join-Path $frontend 'node_modules'
$result = [pscustomobject]@{
    Outcome = $outcome; Error = $errorText; NpmCalls = @($script:NpmCalls | ForEach-Object { ,@($_) });
    ProbeCalls = @($script:ProbeCalls); MarkerWrites = $script:MarkerWrites;
    MarkerExists = (Test-Path -LiteralPath (Join-Path $nodeModules '.job-application-node-dependencies.sha256') -PathType Leaf)
}
$result | ConvertTo-Json -Compress -Depth 6
'''
        script_path = Path(tmp) / "run-test.ps1"
        script_path.write_text(script, encoding="utf-8")
        environment = os.environ.copy()
        environment.update({
            "FIXTURE_ROOT": str(fixture),
            "FORCE_SETUP": str(force).lower(),
            "PROBE_FAIL": str(probe_fails).lower(),
            "NPM_FAIL": str(npm_fails).lower(),
            "REPAIR_TOOLCHAIN": str(repair).lower(),
            "HAS_LOCK": str(has_lock).lower(),
            "RUN_PS1": str(RUN_PS1),
        })
        return subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
            check=False, capture_output=True, text=True, env=environment, timeout=30,
        )


def result_of(completed: subprocess.CompletedProcess[str]) -> dict:
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return __import__("json").loads(completed.stdout.strip().splitlines()[-1])


def test_valid_marker_and_healthy_toolchain_skip_install() -> None:
    result = result_of(run_case())
    assert result["Outcome"] == "success"
    assert result["NpmCalls"] == []
    assert len(result["ProbeCalls"]) == 2
    assert all(Path(call["Path"]).is_absolute() for call in result["ProbeCalls"])


@pytest.mark.parametrize("missing", ["shim_tsc", "shim_vite", "entry_tsc", "entry_vite"])
def test_missing_shim_or_package_entry_triggers_install(missing: str) -> None:
    result = result_of(run_case(missing=missing))
    assert result["Outcome"] == "success"
    assert result["NpmCalls"] == [["ci", "--include=dev", "--bin-links=true"]]
    assert result["MarkerWrites"] == 1


def test_failed_version_probe_triggers_repair_install() -> None:
    result = result_of(run_case(probe_fails=True))
    assert result["Outcome"] == "success"
    assert result["NpmCalls"] == [["ci", "--include=dev", "--bin-links=true"]]
    assert len(result["ProbeCalls"]) == 3


@pytest.mark.parametrize("kwargs", [{"marker": False}, {"force": True}])
def test_missing_marker_or_force_setup_installs(kwargs: dict) -> None:
    result = result_of(run_case(**kwargs))
    assert result["Outcome"] == "success"
    assert result["NpmCalls"] == [["ci", "--include=dev", "--bin-links=true"]]


def test_missing_lock_uses_install_with_dev_and_bin_link_flags() -> None:
    result = result_of(run_case(marker=False, has_lock=False))
    assert result["Outcome"] == "success"
    assert result["NpmCalls"] == [["install", "--include=dev", "--bin-links=true"]]


def test_npm_failure_throws_without_writing_a_marker() -> None:
    result = result_of(run_case(marker=False, npm_fails=True))
    assert result["Outcome"] == "error"
    assert "Installing frontend dependencies failed" in result["Error"]
    assert result["MarkerWrites"] == 0
    assert not result["MarkerExists"]


def test_incomplete_toolchain_after_install_throws_without_writing_a_marker() -> None:
    result = result_of(run_case(marker=False, repair=False, missing="shim_vite"))
    assert result["Outcome"] == "error"
    assert "toolchain is still incomplete" in result["Error"]
    assert result["MarkerWrites"] == 0
    assert not result["MarkerExists"]


def test_path_merge_is_ordered_trimmed_and_case_insensitive() -> None:
    completed = run_path_case()
    result = result_of(completed)
    assert result["Merged"] == r"C:\ProcessOnly;C:\Shared;D:\UserOnly;E:\MachineOnly"
    assert result["AfterFirst"] == result["AfterSecond"]
    assert result["AfterFirst"].startswith(r"C:\CodexProcessOnly;C:\KeepThis")
    assert "" not in result["AfterFirst"].split(";")
    assert len(result["AfterFirst"].split(";")) == len(set(part.casefold() for part in result["AfterFirst"].split(";")))


def run_path_case() -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory(prefix="frontend-path-") as tmp:
        script = r'''
$ErrorActionPreference = 'Stop'
$source = [System.IO.File]::ReadAllText($env:RUN_PS1)
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($source, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -gt 0) { throw "Could not parse run.ps1: $($parseErrors[0].Message)" }
foreach ($name in @('Merge-ProcessPath', 'Refresh-ProcessPath')) {
    $definition = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $true)
    if (-not $definition) { throw "Missing function $name" }
    . ([scriptblock]::Create($definition.Extent.Text))
}
$merged = Merge-ProcessPath @(' C:\ProcessOnly ; C:\Shared;; ', 'c:\shared; D:\UserOnly ', '', 'E:\MachineOnly')
$originalPath = $env:PATH
try {
    $env:PATH = 'C:\CodexProcessOnly; C:\KeepThis ; ; c:\codexprocessonly'
    Refresh-ProcessPath
    $afterFirst = $env:PATH
    Refresh-ProcessPath
    $afterSecond = $env:PATH
} finally { $env:PATH = $originalPath }
[pscustomobject]@{ Merged = $merged; AfterFirst = $afterFirst; AfterSecond = $afterSecond } | ConvertTo-Json -Compress
'''
        script_path = Path(tmp) / "path-test.ps1"
        script_path.write_text(script, encoding="utf-8")
        environment = os.environ.copy()
        environment["RUN_PS1"] = str(RUN_PS1)
        return subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
            check=False, capture_output=True, text=True, env=environment, timeout=30,
        )
