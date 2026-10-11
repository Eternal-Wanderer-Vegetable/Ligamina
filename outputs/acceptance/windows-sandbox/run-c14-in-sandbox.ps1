$ErrorActionPreference = "Stop"
$repoRoot = "C:\Ligamina"
$runDate = (Get-Date).ToString("yyyyMMdd")
$acceptanceScript = Join-Path $repoRoot "scripts\windows-install-acceptance.ps1"
$reportPath = Join-Path $repoRoot "outputs\acceptance\t15-windows-sandbox-c14-$runDate.json"
$environmentReportPath = Join-Path $repoRoot "outputs\acceptance\t15-windows-sandbox-environment-$runDate.json"

if (-not (Test-Path -LiteralPath $acceptanceScript)) {
    throw "Mapped Ligamina checkout is missing the Windows acceptance script: $acceptanceScript"
}

$env:RUNNER_OS = "WindowsSandbox"
$env:RUNNER_TEMP = Join-Path $env:USERPROFILE "L 验收"
New-Item -ItemType Directory -Force -Path $env:RUNNER_TEMP | Out-Null
$cacheRoot = Join-Path $repoRoot ".work\windows-sandbox-cache-20261010"
$env:LIGAMINA_RUNTIME_CACHE = Join-Path $cacheRoot "archives"
$env:LIGAMINA_ACCEPTANCE_ARTIFACT_CACHE = Join-Path $cacheRoot "artifacts"
$env:UV_CACHE_DIR = Join-Path $cacheRoot "uv-cache"
$env:UV_OFFLINE = "1"
$env:npm_config_cache = Join-Path $cacheRoot "npm-cache"
$env:npm_config_offline = "true"
$cachePreparationReportPath = Join-Path $cacheRoot "preparation-report.json"
$cachePreparationReport = if (Test-Path -LiteralPath $cachePreparationReportPath) {
    Get-Content -LiteralPath $cachePreparationReportPath -Raw -Encoding UTF8 | ConvertFrom-Json
}
else {
    $null
}
if ($null -eq $cachePreparationReport -or -not $cachePreparationReport.python_distribution_relative_path) {
    throw "C14 staged runtime cache is missing its managed Python distribution. Run prepare-c14-runtime-cache.ps1 first."
}
$env:LIGAMINA_PYTHON_RUNTIME_CACHE = Join-Path $cacheRoot $cachePreparationReport.python_distribution_relative_path

$runtimeCommands = foreach ($name in @("python.exe", "python3.exe", "node.exe", "npm.cmd", "docker.exe", "uv.exe")) {
    $command = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
    [ordered]@{
        name = $name
        found_on_path = ($null -ne $command)
        path = if ($null -ne $command) { $command.Source } else { $null }
    }
}
$archiveModuleCandidates = @(Get-Module -ListAvailable -Name "Microsoft.PowerShell.Archive" | ForEach-Object {
    [ordered]@{ version = $_.Version.ToString(); path = $_.Path }
})
$archiveModuleProbe = [ordered]@{ status = "not_run"; error = $null; fully_qualified_error_id = $null }
try {
    Import-Module -Name "Microsoft.PowerShell.Archive" -ErrorAction Stop
    $archiveModuleProbe.status = "pass"
}
catch {
    $archiveModuleProbe.status = "fail"
    $archiveModuleProbe.error = $_.Exception.ToString()
    $archiveModuleProbe.fully_qualified_error_id = $_.FullyQualifiedErrorId
}
$runtimeManifest = Get-Content -LiteralPath (Join-Path $repoRoot "deploy\windows\runtime-manifest.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$downloadProbes = foreach ($runtimeName in @("uv", "node")) {
    $runtime = $runtimeManifest.runtimes.$runtimeName
    $uri = [Uri]$runtime.url
    $probe = [ordered]@{
        runtime = $runtimeName
        url = $runtime.url
        resolved_addresses = @()
        http_status = $null
        error = $null
    }
    try {
        $probe.resolved_addresses = @([System.Net.Dns]::GetHostAddresses($uri.DnsSafeHost) | ForEach-Object { $_.IPAddressToString })
        $request = [System.Net.HttpWebRequest]::Create($uri)
        $request.Method = "HEAD"
        $request.Timeout = 5000
        $request.ReadWriteTimeout = 5000
        $response = $request.GetResponse()
        $probe.http_status = [int]$response.StatusCode
        $response.Dispose()
    }
    catch {
        $probe.error = $_.Exception.ToString()
        if ($null -ne $_.Exception.Response) {
            $probe.http_status = [int]$_.Exception.Response.StatusCode
            $_.Exception.Response.Dispose()
        }
    }
    $probe
}
$os = Get-CimInstance Win32_OperatingSystem
$cliPreflightPath = Join-Path $repoRoot ".work\releases\ligamina-win-x64\target\release\ligamina-cli.exe"
$cliPreflight = [ordered]@{ path = $cliPreflightPath; status = "not_run"; exit_code = $null; output = $null; error = $null }
try {
    if (-not (Test-Path -LiteralPath $cliPreflightPath -PathType Leaf)) {
        throw "Packaged CLI is missing from the release bundle."
    }
    $cliPreflight.output = (& $cliPreflightPath --help 2>&1 | Out-String)
    $cliPreflight.exit_code = $LASTEXITCODE
    $cliPreflight.status = if ($cliPreflight.exit_code -eq 0 -and $cliPreflight.output -match "daemon") { "pass" } else { "fail" }
}
catch {
    $cliPreflight.status = "fail"
    $cliPreflight.error = $_.Exception.ToString()
}
$environmentReport = [ordered]@{
    schema_version = 1
    task = "C14"
    environment = "Windows Sandbox"
    captured_at_utc = [DateTime]::UtcNow.ToString("o")
    os_caption = $os.Caption
    os_version = $os.Version
    os_architecture = $os.OSArchitecture
    runtime_commands_before_install = @($runtimeCommands)
    runtime_cache_mode = "staged, SHA-256-verified archives and dependency caches"
    runtime_cache_preparation = $cachePreparationReport
    runtime_archive_cache_path = $env:LIGAMINA_RUNTIME_CACHE
    python_runtime_cache_path = $env:LIGAMINA_PYTHON_RUNTIME_CACHE
    acceptance_artifact_cache_path = $env:LIGAMINA_ACCEPTANCE_ARTIFACT_CACHE
    uv_cache_path = $env:UV_CACHE_DIR
    npm_cache_path = $env:npm_config_cache
    cli_preflight = $cliPreflight
    powershell_version = $PSVersionTable.PSVersion.ToString()
    powershell_module_path = $env:PSModulePath
    archive_module_candidates = $archiveModuleCandidates
    archive_module_probe = $archiveModuleProbe
    runtime_download_probes = @($downloadProbes)
    install_scratch_root = $env:RUNNER_TEMP
    acceptance_report_path = $reportPath
    acceptance_status = "pending"
    failure = $null
}

try {
    & $acceptanceScript -ReportPath $reportPath
    $acceptanceReport = Get-Content -LiteralPath $reportPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($acceptanceReport.status -ne "pass") {
        throw "Windows package acceptance reported status '$($acceptanceReport.status)'."
    }
    $environmentReport.acceptance_status = "pass"
}
catch {
    $environmentReport.acceptance_status = "fail"
    $environmentReport.failure = $_.Exception.Message
    $environmentReport.failure_details = $_.Exception.ToString()
    $environmentReport.failure_fully_qualified_error_id = $_.FullyQualifiedErrorId
    $environmentReport.failure_script_stack_trace = $_.ScriptStackTrace
    $environmentReport.failure_position_message = $_.InvocationInfo.PositionMessage
}

$json = $environmentReport | ConvertTo-Json -Depth 8
[System.IO.File]::WriteAllText($environmentReportPath, $json + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
if ($environmentReport.acceptance_status -ne "pass") {
    throw $environmentReport.failure
}
Write-Output "Clean Windows Sandbox C14 acceptance passed: $reportPath"
