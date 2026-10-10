[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "Programs\Ligamina"),
    [string]$ConfigPath = (Join-Path $env:APPDATA "Ligamina\daemon.json")
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$root = [System.IO.Path]::GetFullPath($InstallRoot)
$activePath = Join-Path $root "active.json"
if (-not (Test-Path -LiteralPath $activePath)) { throw "Ligamina is not installed at '$root'." }
$active = Get-Content -LiteralPath $activePath -Raw | ConvertFrom-Json
$versionRoot = [System.IO.Path]::GetFullPath((Join-Path $root $active.version_path))
$prefix = $root.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
if (-not $versionRoot.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) { throw "The active version path escapes the installation root." }
$cli = Join-Path $versionRoot "ligamina-cli.exe"
if (-not (Test-Path -LiteralPath $cli)) { throw "The active CLI executable is missing: '$cli'" }
$configFull = [System.IO.Path]::GetFullPath($ConfigPath)
if (-not (Test-Path -LiteralPath $configFull)) {
    $configureScript = Join-Path $PSScriptRoot "configure.ps1"
    if (-not (Test-Path -LiteralPath $configureScript)) { throw "The first-run configurator is missing: '$configureScript'." }
    & $configureScript -InstallRoot $root -ConfigPath $configFull
}
if (-not (Test-Path -LiteralPath $configFull)) { throw "No daemon configuration was created at '$configFull'." }
& $cli daemon --config $configFull
exit $LASTEXITCODE
