[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$VaultRoot,

    [string]$ClientId,
    [string]$TenantId,

    # Recommended for corporate tenants: disables every Graph write command.
    [switch]$ReadOnly,

    # v0.6 "cerebro" layout. One profile per Microsoft 365 tenant (1..N per
    # vault). -Profile is an alias so $PROFILE (PowerShell) is not shadowed.
    [Alias("Profile")]
    [string]$ProfileName,
    [string]$Empresa,
    [ValidateSet("legacy", "cerebro")]
    [string]$Layout,
    # Teams transcripts (needs admin consent + the tenant transcript switch).
    [switch]$Teams,
    # Register the automatic sync (Windows: \Rewired\Cerebro - ingesta).
    [switch]$Schedule,
    # Optional overrides of the vault-relative paths (defaults: raw/entradas,
    # .claude/system3/cerebro.lock, .claude/system3/logs/ingesta.log).
    [string]$RawRoot,
    [string]$LockPath,
    [string]$IngestLogPath
)

$ErrorActionPreference = "Stop"

# Names skipped at every directory level. Copying a directory with
# Copy-Item -Recurse into an existing destination nests it (docs\docs) on
# the second run, so directories are recreated and their contents copied.
$ExcludedNames = @(".git", "tests", ".github", "__pycache__")

function Write-Step([string]$Message) {
    Write-Host "`n  -> $Message" -ForegroundColor Cyan
}

function New-DirectoryLiteral([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw "Cannot create an empty directory path."
    }
    [void][System.IO.Directory]::CreateDirectory($Path)
    return [System.IO.Path]::GetFullPath($Path)
}

function Join-Literal([string]$Parent, [string]$Child) {
    # Join-Path has no -LiteralPath on Windows PowerShell 5.1, and -Path
    # treats brackets as wildcards. Combine does not.
    return [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($Parent, $Child))
}

function Copy-TreeLiteral {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$ResolvedTarget
    )
    New-DirectoryLiteral $Destination | Out-Null
    foreach ($item in @(Get-ChildItem -LiteralPath $Source -Force)) {
        if ($ExcludedNames -contains $item.Name) { continue }
        $full = [System.IO.Path]::GetFullPath($item.FullName).TrimEnd([char]'\')
        $itemPrefix = $full + "\"
        if ($ResolvedTarget.StartsWith($itemPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            continue
        }
        $destPath = [System.IO.Path]::Combine($Destination, $item.Name)
        if ($item.PSIsContainer) {
            Copy-TreeLiteral -Source $item.FullName -Destination $destPath -ResolvedTarget $ResolvedTarget
        } else {
            Copy-Item -LiteralPath $item.FullName -Destination $destPath -Force
        }
    }
}

function Find-RealPython {
    $candidates = @(
        @{ Exe = "py"; Prefix = @("-3") },
        @{ Exe = "python"; Prefix = @() }
    )

    foreach ($candidate in $candidates) {
        $commands = @(Get-Command $candidate.Exe -All -ErrorAction SilentlyContinue)
        foreach ($command in $commands) {
            # ApplicationInfo.Path is the executable. Some command types only
            # fill Source. The Store stub lives under WindowsApps and is not Python.
            $path = [string]$command.Path
            if ([string]::IsNullOrWhiteSpace($path)) {
                $path = [string]$command.Source
            }
            if ([string]::IsNullOrWhiteSpace($path)) { continue }
            if ($path -like '*\WindowsApps\*') { continue }
            $checkArgs = @($candidate.Prefix) + @("-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)")
            & $path @checkArgs 2>$null
            if ($LASTEXITCODE -eq 0) {
                return @{ Exe = $path; Prefix = $candidate.Prefix }
            }
        }
    }
    throw "Python 3.10+ was not found. Install it for your user from python.org and enable Add python.exe to PATH."
}

Write-Host "ingest-outlook Windows installer"
Write-Host "================================"

if ($Layout -eq "cerebro" -and [string]::IsNullOrWhiteSpace($Empresa)) {
    throw "-Layout cerebro requires -Empresa <slug> (the short company name written in every raw file)."
}
if (-not [string]::IsNullOrWhiteSpace($ProfileName) -and $ProfileName -cnotmatch '^[a-z0-9-]{1,32}$') {
    throw "-Profile must be a slug: lowercase letters, digits and dashes (max 32)."
}

Write-Step "Detecting Python 3.10+"
$Python = Find-RealPython
$versionArgs = @($Python.Prefix) + "--version"
$PythonVersion = & $Python.Exe @versionArgs 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "Python --version failed with exit code $LASTEXITCODE. Output: $PythonVersion"
}
Write-Host "[ok] $PythonVersion"

$ResolvedVault = New-DirectoryLiteral $VaultRoot
# "C:\vault\" would reach Python as C:\vault" (a trailing backslash escapes
# the closing quote on the Windows command line). Keep drive roots (C:\) intact.
$VaultArg = $ResolvedVault
if ($VaultArg.Length -gt 3) { $VaultArg = $VaultArg.TrimEnd([char]'\') }
$TargetDir = Join-Literal $ResolvedVault ".claude\skills\ingest-outlook"
$SourceDir = [System.IO.Path]::GetFullPath($PSScriptRoot)

Write-Step "Installing the skill in the vault"
$targetExists = Test-Path -LiteralPath $TargetDir
$ResolvedTarget = if ($targetExists) {
    [System.IO.Path]::GetFullPath((Get-Item -LiteralPath $TargetDir).FullName)
} else {
    [System.IO.Path]::GetFullPath($TargetDir)
}
if (-not $SourceDir.TrimEnd([char]'\').Equals($ResolvedTarget.TrimEnd([char]'\'), [System.StringComparison]::OrdinalIgnoreCase)) {
    Copy-TreeLiteral -Source $SourceDir -Destination $TargetDir -ResolvedTarget $ResolvedTarget
    Write-Host "[ok] Copied repository files to $TargetDir"
} else {
    Write-Host "[ok] Already running from $TargetDir"
}

if (-not [string]::IsNullOrWhiteSpace($env:INGEST_OUTLOOK_CONFIG_DIR)) {
    $ConfigDir = [System.IO.Path]::GetFullPath($env:INGEST_OUTLOOK_CONFIG_DIR)
} else {
    $ConfigDir = Join-Literal $env:USERPROFILE ".config\ingest-outlook"
}
if (-not [string]::IsNullOrWhiteSpace($ProfileName)) {
    $ConfigDir = Join-Literal $ConfigDir $ProfileName
}
Write-Step "Preparing configuration in $ConfigDir"
New-DirectoryLiteral $ConfigDir | Out-Null

$Fetch = Join-Literal $TargetDir "fetch.py"
if (-not (Test-Path -LiteralPath $Fetch)) {
    throw "fetch.py was not installed at $Fetch"
}
$ConfigureArgs = @($Fetch)
if ($ProfileName) { $ConfigureArgs += @("--profile", $ProfileName) }
$ConfigureArgs += @("configure", "--vault-root", $VaultArg)
if ($ClientId) { $ConfigureArgs += @("--client-id", $ClientId) }
if ($TenantId) { $ConfigureArgs += @("--tenant-id", $TenantId) }
if ($ReadOnly) { $ConfigureArgs += "--read-only" }
if ($Layout) { $ConfigureArgs += @("--layout", $Layout) }
if ($Empresa) { $ConfigureArgs += @("--empresa", $Empresa) }
if ($Teams) { $ConfigureArgs += "--teams" }
if ($RawRoot) { $ConfigureArgs += @("--raw-root", $RawRoot) }
if ($LockPath) { $ConfigureArgs += @("--lock-path", $LockPath) }
if ($IngestLogPath) { $ConfigureArgs += @("--ingest-log-path", $IngestLogPath) }
$runConfigureArgs = @($Python.Prefix) + $ConfigureArgs
& $Python.Exe @runConfigureArgs
if ($LASTEXITCODE -ne 0) { throw "fetch.py configure failed with exit code $LASTEXITCODE" }

Write-Step "Smoke testing fetch.py"
$runVersionArgs = @($Python.Prefix) + @($Fetch, "version")
& $Python.Exe @runVersionArgs
if ($LASTEXITCODE -ne 0) { throw "fetch.py version failed with exit code $LASTEXITCODE" }

if ($Schedule) {
    Write-Step "Scheduling the automatic sync (one task per vault: sync --all)"
    $runScheduleArgs = @($Python.Prefix) + @($Fetch, "schedule", "install", "--vault-root", $VaultArg)
    & $Python.Exe @runScheduleArgs
    $scheduleCode = $LASTEXITCODE
    if ($scheduleCode -ne 0) {
        # Never fatal: the connector is installed; only the automatic run is
        # missing, and docs\respaldo-hook-inicio.md covers that case.
        $Respaldo = Join-Literal $TargetDir "docs\respaldo-hook-inicio.md"
        Write-Warning ("No se pudo programar la ingesta automática (código $scheduleCode). " +
            "El conector quedó instalado, pero la ingesta no correrá sola cada 30 minutos. " +
            "Respaldo: dispararla desde el hook de inicio de Claude Code, como explica $Respaldo")
    }
}

$PythonCommand = if ($Python.Prefix.Count -gt 0) { "$($Python.Exe) $($Python.Prefix -join ' ')" } else { $Python.Exe }
Write-Host "`nInstallation complete." -ForegroundColor Green
Write-Host "For the first Microsoft login, run:"
if ($ProfileName) {
    Write-Host "  $PythonCommand `"$Fetch`" fix --profile $ProfileName"
} else {
    Write-Host "  $PythonCommand `"$Fetch`" fix"
}
