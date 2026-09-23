[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$VaultRoot,

    [string]$ClientId,
    [string]$TenantId,

    # Recommended for corporate tenants: disables every Graph write command.
    [switch]$ReadOnly
)

$ErrorActionPreference = "Stop"

function Write-Step([string]$Message) {
    Write-Host "`n  -> $Message" -ForegroundColor Cyan
}

function Find-RealPython {
    $candidates = @(
        @{ Exe = "py"; Prefix = @("-3") },
        @{ Exe = "python"; Prefix = @() }
    )

    foreach ($candidate in $candidates) {
        $command = Get-Command $candidate.Exe -ErrorAction SilentlyContinue
        if ($null -eq $command) { continue }
        $source = [string]$command.Source
        if ($source -match "\\WindowsApps\\") { continue }
        $checkArgs = @($candidate.Prefix) + @("-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)")
        & $candidate.Exe @checkArgs 2>$null
        if ($LASTEXITCODE -eq 0) {
            return $candidate
        }
    }
    throw "Python 3.10+ was not found. Install it for your user from python.org and enable Add python.exe to PATH."
}

Write-Host "ingest-outlook Windows installer"
Write-Host "================================"

Write-Step "Detecting Python 3.10+"
$Python = Find-RealPython
$versionArgs = @($Python.Prefix) + "--version"
$PythonVersion = & $Python.Exe @versionArgs 2>&1
Write-Host "[ok] $PythonVersion"

$ResolvedVault = [System.IO.Path]::GetFullPath((New-Item -ItemType Directory -Force -Path $VaultRoot).FullName)
$TargetDir = Join-Path $ResolvedVault ".claude\skills\ingest-outlook"
$SourceDir = [System.IO.Path]::GetFullPath($PSScriptRoot)

Write-Step "Installing the skill in the vault"
$targetExists = Test-Path -LiteralPath $TargetDir
$ResolvedTarget = if ($targetExists) { [System.IO.Path]::GetFullPath((Get-Item -LiteralPath $TargetDir).FullName) } else { [System.IO.Path]::GetFullPath($TargetDir) }
if (-not $SourceDir.TrimEnd([char]'\').Equals($ResolvedTarget.TrimEnd([char]'\'), [System.StringComparison]::OrdinalIgnoreCase)) {
    # Capture the source list before creating the target, so a target located
    # below the source cannot be picked up recursively during the copy.
    $SourceItems = @(Get-ChildItem -LiteralPath $SourceDir -Force | Where-Object { $_.Name -ne ".git" })
    New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null
    $SourceItems | ForEach-Object {
        $itemPrefix = [System.IO.Path]::GetFullPath($_.FullName).TrimEnd([char]'\') + "\"
        if (-not $ResolvedTarget.StartsWith($itemPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            Copy-Item -LiteralPath $_.FullName -Destination $TargetDir -Recurse -Force
        }
    }
    Write-Host "[ok] Copied repository files to $TargetDir"
} else {
    Write-Host "[ok] Already running from $TargetDir"
}

$ConfigDir = Join-Path $env:USERPROFILE ".config\ingest-outlook"
Write-Step "Preparing configuration in $ConfigDir"
New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null

$Fetch = Join-Path $TargetDir "fetch.py"
$ConfigureArgs = @($Fetch, "configure", "--vault-root", $ResolvedVault)
if ($ClientId) { $ConfigureArgs += @("--client-id", $ClientId) }
if ($TenantId) { $ConfigureArgs += @("--tenant-id", $TenantId) }
if ($ReadOnly) { $ConfigureArgs += "--read-only" }
$runConfigureArgs = @($Python.Prefix) + $ConfigureArgs
& $Python.Exe @runConfigureArgs
if ($LASTEXITCODE -ne 0) { throw "fetch.py configure failed with exit code $LASTEXITCODE" }

Write-Step "Smoke testing fetch.py"
$runVersionArgs = @($Python.Prefix) + @($Fetch, "version")
& $Python.Exe @runVersionArgs
if ($LASTEXITCODE -ne 0) { throw "fetch.py version failed with exit code $LASTEXITCODE" }

$PythonCommand = if ($Python.Prefix.Count -gt 0) { "$($Python.Exe) $($Python.Prefix -join ' ')" } else { $Python.Exe }
Write-Host "`nInstallation complete." -ForegroundColor Green
Write-Host "For the first Microsoft login, run:"
Write-Host "  $PythonCommand `"$Fetch`" fix"
