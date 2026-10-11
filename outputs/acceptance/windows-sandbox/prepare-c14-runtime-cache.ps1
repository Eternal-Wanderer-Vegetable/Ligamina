[CmdletBinding()]
param(
    [string]$RepositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = [System.IO.Path]::GetFullPath($RepositoryRoot)
$cacheRoot = Join-Path $repoRoot ".work\windows-sandbox-cache-20261010"
$archivesRoot = Join-Path $cacheRoot "archives"
$artifactsRoot = Join-Path $cacheRoot "artifacts"
$uvRuntimeRoot = Join-Path $cacheRoot "uv-runtime"
$uvCacheRoot = Join-Path $cacheRoot "uv-cache"
$npmCacheRoot = Join-Path $cacheRoot "npm-cache"
$stamp = Get-Date -Format "yyyyMMddHHmmss"
$pythonStageRoot = Join-Path $cacheRoot "python-stage-$stamp"
$nonebotStageRoot = Join-Path $cacheRoot "nonebot-stage-$stamp"
$nonebotAutohelpStageRoot = Join-Path $cacheRoot "nonebot-autohelp-stage-$stamp"
$nonebotAutohelpOfflineStageRoot = Join-Path $cacheRoot "nonebot-autohelp-offline-stage-$stamp"
$koishiStageRoot = Join-Path $cacheRoot "koishi-stage-$stamp"
$reportPath = Join-Path $cacheRoot "preparation-report.json"

New-Item -ItemType Directory -Force -Path $archivesRoot, $artifactsRoot, $uvRuntimeRoot, $uvCacheRoot, $npmCacheRoot, $pythonStageRoot, $nonebotStageRoot, $nonebotAutohelpStageRoot, $nonebotAutohelpOfflineStageRoot, $koishiStageRoot | Out-Null
$manifest = Get-Content -LiteralPath (Join-Path $repoRoot "deploy\windows\runtime-manifest.json") -Raw | ConvertFrom-Json
$archiveRecords = @()
foreach ($runtimeName in @("uv", "node")) {
    $runtime = $manifest.runtimes.$runtimeName
    $archivePath = Join-Path $archivesRoot $runtime.archive
    if (-not (Test-Path -LiteralPath $archivePath -PathType Leaf)) {
        Invoke-WebRequest -UseBasicParsing -Uri $runtime.url -OutFile $archivePath -TimeoutSec 300
    }
    $actualHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $runtime.sha256) {
        throw "Staged $runtimeName archive SHA-256 mismatch: expected $($runtime.sha256), got $actualHash."
    }
    $archiveRecords += [ordered]@{
        runtime = $runtimeName
        file = $runtime.archive
        sha256 = $actualHash
        source_url = $runtime.url
    }
}

$uvArchive = Join-Path $archivesRoot $manifest.runtimes.uv.archive
if (-not (Test-Path -LiteralPath (Join-Path $uvRuntimeRoot "uv.exe"))) {
    Expand-Archive -LiteralPath $uvArchive -DestinationPath $uvRuntimeRoot -Force
}
$uvExe = Join-Path $uvRuntimeRoot "uv.exe"
if (-not (Test-Path -LiteralPath $uvExe)) { throw "The staged uv archive did not contain uv.exe." }
$uvVersion = (& $uvExe --version | Out-String).Trim()
if ($uvVersion -notmatch [regex]::Escape($manifest.runtimes.uv.version)) {
    throw "Unexpected staged uv version '$uvVersion'."
}

$savedEnvironment = @{
    UV_CACHE_DIR = $env:UV_CACHE_DIR
    UV_PROJECT_ENVIRONMENT = $env:UV_PROJECT_ENVIRONMENT
    UV_OFFLINE = $env:UV_OFFLINE
    UV_NO_PROGRESS = $env:UV_NO_PROGRESS
    npm_config_cache = $env:npm_config_cache
    npm_config_offline = $env:npm_config_offline
}
try {
    $env:UV_CACHE_DIR = $uvCacheRoot
    $env:UV_NO_PROGRESS = "1"
    Remove-Item Env:\UV_OFFLINE -ErrorAction SilentlyContinue
    $uvPythonArgs = @("python", "install", $manifest.runtimes.python.version, "--install-dir", $pythonStageRoot, "--no-registry", "--no-bin", "--no-progress")
    & $uvExe @uvPythonArgs
    if ($LASTEXITCODE -ne 0) { throw "uv Python cache preparation failed with exit code $LASTEXITCODE." }
    $pythonExe = Get-ChildItem -LiteralPath $pythonStageRoot -Recurse -File -Filter "python.exe" |
        Where-Object { $_.FullName -notmatch "\\Lib\\" } |
        Select-Object -First 1 -ExpandProperty FullName
    if (-not $pythonExe) { throw "uv did not stage a Python interpreter for the dependency cache." }

    $env:UV_PROJECT_ENVIRONMENT = $nonebotStageRoot
    & $uvExe sync --project (Join-Path $repoRoot "hosts\nonebot") --locked --no-dev --python $pythonExe
    if ($LASTEXITCODE -ne 0) { throw "NoneBot dependency cache preparation failed with exit code $LASTEXITCODE." }

    $sampleProfileRoot = Join-Path $repoRoot "outputs\acceptance\t13-representative-wsl-final-20261010-locks\nonebot-autohelp"
    $sampleLockPath = Join-Path $sampleProfileRoot "uv.lock"
    if (-not (Test-Path -LiteralPath $sampleLockPath -PathType Leaf)) {
        throw "The hash-verified T13 NoneBot autohelp lock profile is missing."
    }
    $env:UV_PROJECT_ENVIRONMENT = $nonebotAutohelpStageRoot
    & $uvExe sync --project $sampleProfileRoot --locked --no-dev --no-build --no-progress --python $pythonExe
    if ($LASTEXITCODE -ne 0) { throw "NoneBot autohelp lock-profile cache preparation failed with exit code $LASTEXITCODE." }

    $env:UV_PROJECT_ENVIRONMENT = $nonebotAutohelpOfflineStageRoot
    $env:UV_OFFLINE = "1"
    & $uvExe sync --project $sampleProfileRoot --locked --no-dev --no-build --no-progress --python $pythonExe --offline
    if ($LASTEXITCODE -ne 0) { throw "Offline NoneBot autohelp lock-profile cache verification failed with exit code $LASTEXITCODE." }
    Remove-Item Env:\UV_OFFLINE -ErrorAction SilentlyContinue

    $npmCommand = (Get-Command "npm.cmd" -ErrorAction Stop).Source
    $env:npm_config_cache = $npmCacheRoot
    Remove-Item Env:\npm_config_offline -ErrorAction SilentlyContinue
    Copy-Item -LiteralPath (Join-Path $repoRoot "hosts\koishi\package.json"), (Join-Path $repoRoot "hosts\koishi\package-lock.json") -Destination $koishiStageRoot -Force
    Push-Location $koishiStageRoot
    try {
        & $npmCommand ci --omit=dev --cache $npmCacheRoot
        if ($LASTEXITCODE -ne 0) { throw "Koishi dependency cache preparation failed with exit code $LASTEXITCODE." }
    }
    finally { Pop-Location }
}
finally {
    foreach ($name in $savedEnvironment.Keys) {
        $oldValue = $savedEnvironment[$name]
        if ($null -eq $oldValue) { Remove-Item "Env:\$name" -ErrorAction SilentlyContinue }
        else { Set-Item "Env:\$name" -Value $oldValue }
    }
}

$sampleManifest = Get-Content -LiteralPath (Join-Path $repoRoot "samples\third-party.lock.json") -Raw | ConvertFrom-Json
$sample = $sampleManifest.samples | Where-Object sample_id -eq "nonebot-autohelp"
if ($null -eq $sample) { throw "The pinned NoneBot autohelp acceptance sample is missing." }
$wheelName = [System.IO.Path]::GetFileName(([Uri]$sample.artifact_url).AbsolutePath)
$wheelPath = Join-Path $artifactsRoot $wheelName
if (-not (Test-Path -LiteralPath $wheelPath -PathType Leaf)) {
    Invoke-WebRequest -UseBasicParsing -Uri $sample.artifact_url -OutFile $wheelPath -TimeoutSec 180
}
$wheelHash = (Get-FileHash -LiteralPath $wheelPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($wheelHash -ne $sample.artifact_hash) {
    throw "Staged autohelp wheel SHA-256 mismatch: expected $($sample.artifact_hash), got $wheelHash."
}

$report = [ordered]@{
    schema_version = 1
    task = "C14"
    prepared_at_utc = [DateTime]::UtcNow.ToString("o")
    cache_mode = "host-downloaded pinned artifacts and host-warmed locked dependency caches, reverified in Windows Sandbox"
    host_network_proxy_endpoint_not_recorded = $true
    runtime_archives = @($archiveRecords)
    python_version = $manifest.runtimes.python.version
    staged_python_version = (& $pythonExe --version | Out-String).Trim()
    python_stage_path = $pythonStageRoot
    python_distribution_path = (Split-Path -Parent $pythonExe)
    python_distribution_relative_path = (Join-Path (Split-Path -Leaf $pythonStageRoot) (Split-Path -Leaf (Split-Path -Parent $pythonExe)))
    uv_version = $uvVersion
    node_version = $manifest.runtimes.node.version
    nonebot_autohelp_lock_profile = [ordered]@{
        project = "outputs/acceptance/t13-representative-wsl-final-20261010-locks/nonebot-autohelp"
        lock_sha256 = (Get-FileHash -LiteralPath (Join-Path $repoRoot "outputs\acceptance\t13-representative-wsl-final-20261010-locks\nonebot-autohelp\uv.lock") -Algorithm SHA256).Hash.ToLowerInvariant()
        cache_preparation_offline_verification = "pass"
    }
    autohelp_wheel = [ordered]@{ file = $wheelName; sha256 = $wheelHash; source_url = $sample.artifact_url }
    uv_cache_path = $uvCacheRoot
    npm_cache_path = $npmCacheRoot
}
$json = $report | ConvertTo-Json -Depth 8
[System.IO.File]::WriteAllText($reportPath, $json + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
Write-Output "C14 runtime and dependency caches are ready: $reportPath"
