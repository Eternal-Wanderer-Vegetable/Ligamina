[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "Programs\Ligamina"),
    [string]$ConfigPath = (Join-Path $env:APPDATA "Ligamina\daemon.json")
)

$ErrorActionPreference = "Stop"
$root = [System.IO.Path]::GetFullPath($InstallRoot)
$activePath = Join-Path $root "active.json"
if (-not (Test-Path -LiteralPath $activePath)) { throw "Ligamina is not installed at '$root'." }
if (-not (Test-Path -LiteralPath $ConfigPath)) { throw "Create a daemon config at '$ConfigPath' before starting Ligamina." }
$active = Get-Content -LiteralPath $activePath -Raw | ConvertFrom-Json
$versionRoot = [System.IO.Path]::GetFullPath((Join-Path $root $active.version_path))
$prefix = $root.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
if (-not $versionRoot.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) { throw "The active version path escapes the installation root." }
$cli = Join-Path $versionRoot "ligamina-cli.exe"
if (-not (Test-Path -LiteralPath $cli)) { throw "The active CLI executable is missing: '$cli'" }
& $cli daemon --config ([System.IO.Path]::GetFullPath($ConfigPath))
exit $LASTEXITCODE
