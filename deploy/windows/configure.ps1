[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "Programs\Ligamina"),
    [string]$ConfigPath = (Join-Path $env:APPDATA "Ligamina\daemon.json"),
    [string]$UserDataRoot = (Join-Path $env:APPDATA "Ligamina"),
    [string[]]$Profiles
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function New-RandomToken {
    $bytes = New-Object byte[] 32
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($bytes) }
    finally { $generator.Dispose() }
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function New-HostConfiguration(
    [string]$Id,
    [string]$Program,
    [string[]]$Arguments,
    [string]$CurrentDirectory,
    [hashtable]$Environment
) {
    return [ordered]@{
        id = $Id
        enabled = $true
        program = $Program
        args = $Arguments
        current_dir = $CurrentDirectory
        environment = $Environment
        max_restarts = 3
        restart_backoff_ms = 1000
        startup_timeout_ms = 60000
        shutdown_timeout_ms = 10000
        operation_grants = @()
    }
}

$installFull = [System.IO.Path]::GetFullPath($InstallRoot)
$configFull = [System.IO.Path]::GetFullPath($ConfigPath)
if (Test-Path -LiteralPath $configFull) {
    throw "Configuration already exists at '$configFull'; refusing to overwrite it."
}

$activePath = Join-Path $installFull "active.json"
if (-not (Test-Path -LiteralPath $activePath)) {
    throw "Ligamina is not installed at '$installFull'."
}
$active = Get-Content -LiteralPath $activePath -Raw | ConvertFrom-Json
$versionRoot = [System.IO.Path]::GetFullPath((Join-Path $installFull $active.version_path))
$installPrefix = $installFull.TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
if (-not $versionRoot.StartsWith($installPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "The active version path escapes the installation root."
}

$availableProfiles = @($active.profiles | ForEach-Object { "$($_)".Trim().ToLowerInvariant() } | Where-Object { $_ } | Sort-Object -Unique)
if ($availableProfiles.Count -eq 0 -or @($availableProfiles | Where-Object { $_ -notin @("nonebot", "koishi") }).Count -gt 0) {
    throw "The active installation does not contain a supported Host profile."
}

if ($null -eq $Profiles -or $Profiles.Count -eq 0) {
    Write-Host "First-run setup creates a loopback-only Core configuration with no platform operation grants."
    Write-Host ("Installed Host profiles: " + ($availableProfiles -join ", "))
    $profileInput = Read-Host "Host profiles to start (comma-separated; Enter selects all installed profiles)"
    if ([string]::IsNullOrWhiteSpace($profileInput)) {
        $selectedProfiles = $availableProfiles
    }
    else {
        $selectedProfiles = @($profileInput.Split(',') | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ } | Sort-Object -Unique)
    }
}
else {
    $selectedProfiles = @($Profiles | ForEach-Object { $_.Split(',') } | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ } | Sort-Object -Unique)
}

if ($selectedProfiles.Count -eq 0 -or @($selectedProfiles | Where-Object { $_ -notin $availableProfiles }).Count -gt 0) {
    throw "Choose one or more profiles present in the active installation: $($availableProfiles -join ', ')."
}

if ($null -eq $Profiles -or $Profiles.Count -eq 0) {
    $confirmation = Read-Host ("Create the configuration for " + ($selectedProfiles -join ", ") + " and start Core on 127.0.0.1:26701? [Y/n]")
    if ($confirmation -and $confirmation.Trim() -notmatch '^(?i:y|yes)$') {
        throw "First-run setup was cancelled; no configuration was written."
    }
}

$userDataRoot = [System.IO.Path]::GetFullPath($UserDataRoot)
$hostDataRoot = Join-Path $userDataRoot "hosts"
$hosts = [System.Collections.Generic.List[object]]::new()

if ($selectedProfiles -contains "nonebot") {
    $hostRoot = Join-Path $versionRoot "hosts\nonebot"
    $pythonExe = Join-Path $hostRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $pythonExe)) { throw "The installed NoneBot runtime is missing: '$pythonExe'." }
    $dataRoot = Join-Path $hostDataRoot "nonebot"
    New-Item -ItemType Directory -Force -Path $dataRoot | Out-Null
    $hosts.Add((New-HostConfiguration -Id "nonebot-default" -Program $pythonExe -Arguments @("-m", "ligamina_nonebot.worker") -CurrentDirectory $hostRoot -Environment @{ LIGAMINA_NONEBOT_DATA_DIR = $dataRoot }))
}

if ($selectedProfiles -contains "koishi") {
    $hostRoot = Join-Path $versionRoot "hosts\koishi"
    $runtimeEvidencePath = Join-Path $versionRoot "runtime-evidence.json"
    if (-not (Test-Path -LiteralPath $runtimeEvidencePath)) { throw "Runtime evidence is missing from the active version: '$runtimeEvidencePath'." }
    $runtimeEvidence = Get-Content -LiteralPath $runtimeEvidencePath -Raw | ConvertFrom-Json
    $nodeVersion = "$($runtimeEvidence.runtimes.node.version)"
    if (-not $nodeVersion) { throw "The active version does not record a Node runtime version." }
    $nodeExe = Join-Path $versionRoot "runtimes\node-v$nodeVersion\node.exe"
    $workerScript = Join-Path $hostRoot "dist\worker.js"
    if (-not (Test-Path -LiteralPath $nodeExe)) { throw "The installed Node runtime is missing: '$nodeExe'." }
    if (-not (Test-Path -LiteralPath $workerScript)) { throw "The installed Koishi worker is missing: '$workerScript'." }
    $dataRoot = Join-Path $hostDataRoot "koishi"
    New-Item -ItemType Directory -Force -Path $dataRoot | Out-Null
    $hosts.Add((New-HostConfiguration -Id "koishi-default" -Program $nodeExe -Arguments @($workerScript) -CurrentDirectory $hostRoot -Environment @{ LIGAMINA_KOISHI_DATA_DIR = $dataRoot }))
}

$adminToken = New-RandomToken
$clientId = "ligamina-local-client"
$clientToken = New-RandomToken
$gatewayId = "ligamina-local-gateway"
$config = [ordered]@{
    listen = "127.0.0.1:26701"
    allow_non_loopback_listen = $false
    database_path = (Join-Path $userDataRoot "ligamina.sqlite")
    admin_token = $adminToken
    mcp_client_id = $clientId
    clients = @([ordered]@{ id = $clientId; token = $clientToken; capabilities = @(); operation_grants = @() })
    gateways = @([ordered]@{ id = $gatewayId; client_id = $clientId; account_id = "local-account"; registration_token = (New-RandomToken) })
    hosts = @($hosts.ToArray())
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $configFull), $userDataRoot | Out-Null
$temporaryPath = "$configFull.$([guid]::NewGuid().ToString('N')).tmp"
$encoded = $config | ConvertTo-Json -Depth 16
[System.IO.File]::WriteAllText($temporaryPath, $encoded + [Environment]::NewLine, (New-Object System.Text.UTF8Encoding($false)))
try {
    if (Test-Path -LiteralPath $configFull) { throw "Configuration appeared during setup; refusing to overwrite '$configFull'." }
    [System.IO.File]::Move($temporaryPath, $configFull)
}
catch {
    Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
    throw
}

Write-Host "Created first-run configuration: $configFull"
Write-Host "Enabled Host profiles: $($selectedProfiles -join ', ')"
Write-Host "Core listens on 127.0.0.1:26701. No platform operation grants were created."
Write-Host "The generated administrator token is stored in the configuration file; keep that file private."
