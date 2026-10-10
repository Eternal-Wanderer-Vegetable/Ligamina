[CmdletBinding()]
param(
    [string]$RepositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string]$OutputRoot = (Join-Path (Join-Path (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path ".work") "releases\ligamina-win-x64")
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repo = [System.IO.Path]::GetFullPath($RepositoryRoot)
$output = [System.IO.Path]::GetFullPath($OutputRoot)
$workRoot = [System.IO.Path]::GetFullPath((Join-Path $repo ".work"))
$outputPrefix = $workRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
if (-not $output.StartsWith($outputPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Build output must stay under '$workRoot'."
}

$cargoExe = (Get-Command cargo -ErrorAction Stop).Source
$npmExe = (Get-Command npm -ErrorAction Stop).Source
$nodeExe = (Get-Command node -ErrorAction Stop).Source
$manifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot "runtime-manifest.json") -Raw | ConvertFrom-Json
$nodeVersion = (& $nodeExe --version | Out-String).Trim()
if ($nodeVersion -ne "v$($manifest.runtimes.node.version)") {
    throw "The bundle build requires Node v$($manifest.runtimes.node.version); found '$nodeVersion'."
}

Push-Location $repo
try {
    & $cargoExe build --release -p ligamina-cli --locked --offline
    if ($LASTEXITCODE -ne 0) { throw "Cargo release build failed with exit code $LASTEXITCODE." }
}
finally { Pop-Location }

$koishiRoot = Join-Path $repo "hosts\koishi"
Push-Location $koishiRoot
try {
    & $npmExe ci
    if ($LASTEXITCODE -ne 0) { throw "npm ci failed with exit code $LASTEXITCODE." }
    & $npmExe run build
    if ($LASTEXITCODE -ne 0) { throw "Koishi TypeScript build failed with exit code $LASTEXITCODE." }
}
finally { Pop-Location }

$cliSource = Join-Path $repo "target\release\ligamina-cli.exe"
$nonebotSource = Join-Path $repo "hosts\nonebot"
$koishiDist = Join-Path $koishiRoot "dist"
foreach ($required in @(
    $cliSource,
    (Join-Path $nonebotSource "pyproject.toml"),
    (Join-Path $nonebotSource "uv.lock"),
    (Join-Path $nonebotSource "ligamina_nonebot\worker.py"),
    (Join-Path $koishiRoot "package.json"),
    (Join-Path $koishiRoot "package-lock.json"),
    (Join-Path $koishiDist "worker.js")
)) {
    if (-not (Test-Path -LiteralPath $required)) { throw "Required bundle input is missing: '$required'." }
}

if (Test-Path -LiteralPath $output) {
    $resolvedOutput = (Resolve-Path -LiteralPath $output).Path
    if (-not $resolvedOutput.StartsWith($outputPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to replace an output outside '$workRoot'."
    }
    Remove-Item -LiteralPath $resolvedOutput -Recurse -Force
}

$payloadNonebot = Join-Path $output "hosts\nonebot"
$payloadKoishi = Join-Path $output "hosts\koishi"
$payloadDeploy = Join-Path $output "deploy\windows"
New-Item -ItemType Directory -Force -Path $payloadNonebot, $payloadKoishi, $payloadDeploy, (Join-Path $output "target\release") | Out-Null
Copy-Item -LiteralPath $cliSource -Destination (Join-Path $output "target\release\ligamina-cli.exe") -Force
Copy-Item -LiteralPath (Join-Path $nonebotSource "pyproject.toml"), (Join-Path $nonebotSource "uv.lock") -Destination $payloadNonebot -Force
Copy-Item -LiteralPath (Join-Path $nonebotSource "ligamina_nonebot") -Destination $payloadNonebot -Recurse -Force
Copy-Item -LiteralPath (Join-Path $koishiRoot "package.json"), (Join-Path $koishiRoot "package-lock.json") -Destination $payloadKoishi -Force
Copy-Item -LiteralPath $koishiDist -Destination $payloadKoishi -Recurse -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "runtime-manifest.json"), (Join-Path $PSScriptRoot "install.ps1"), (Join-Path $PSScriptRoot "update.ps1"), (Join-Path $PSScriptRoot "uninstall.ps1"), (Join-Path $PSScriptRoot "run.ps1") -Destination $payloadDeploy -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "licenses") -Destination $payloadDeploy -Recurse -Force
Copy-Item -LiteralPath (Join-Path $repo "README.md") -Destination $output -Force

$cliHash = (Get-FileHash -LiteralPath (Join-Path $output "target\release\ligamina-cli.exe") -Algorithm SHA256).Hash.ToLowerInvariant()
$bundleInfo = [ordered]@{
    schema_version = 1
    application_version = $manifest.application_version
    platform = "windows-x64"
    core_sha256 = $cliHash
    python_profile = "3.12.14"
    node_profile = $manifest.runtimes.node.version
    created_at = [DateTime]::UtcNow.ToString("o")
}
$bundleInfo | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $output "bundle-info.json") -Encoding UTF8
Write-Output "Windows x64 bundle staged at: $output"
Write-Output "Install with: .\deploy\windows\install.ps1 -PayloadRoot `"$output`""
