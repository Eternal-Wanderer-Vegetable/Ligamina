[CmdletBinding()]
param(
    [string]$PayloadRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "Programs\Ligamina"),
    [string[]]$Profile = @("nonebot", "koishi"),
    [string]$ManifestPath = (Join-Path $PSScriptRoot "runtime-manifest.json")
)

$ErrorActionPreference = "Stop"
$activePath = Join-Path ([System.IO.Path]::GetFullPath($InstallRoot)) "active.json"
if (-not (Test-Path -LiteralPath $activePath)) {
    throw "No active Ligamina installation exists at '$InstallRoot'; use install.ps1 first."
}

# install.ps1 provisions and validates a new version in a fresh directory. It only
# swaps active.json after every selected runtime has passed its version checks.
& (Join-Path $PSScriptRoot "install.ps1") -PayloadRoot $PayloadRoot -InstallRoot $InstallRoot -Profile $Profile -ManifestPath $ManifestPath
if (-not $?) { throw "The candidate update did not activate." }
