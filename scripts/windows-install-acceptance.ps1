[CmdletBinding()]
param(
    [string]$ReportPath = (Join-Path $PSScriptRoot "..\outputs\acceptance\windows-install-acceptance.json")
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$payloadRoot = Join-Path $repoRoot ".work\releases\ligamina-win-x64"
$runnerRoot = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { Join-Path $repoRoot ".work" }
$testRoot = Join-Path $runnerRoot ("ligamina-install-acceptance-" + [guid]::NewGuid().ToString("N"))
$installRoot = Join-Path $testRoot "Ligamina-install"
$appDataRoot = Join-Path $testRoot "appdata"
$userDataRoot = Join-Path $appDataRoot "Ligamina"
$configPath = Join-Path $userDataRoot "daemon.json"
$stdoutPath = Join-Path $testRoot "daemon.stdout.log"
$stderrPath = Join-Path $testRoot "daemon.stderr.log"
$checks = [System.Collections.Generic.List[object]]::new()
$failure = $null
$daemonProcess = $null
$hostProcessIds = @()
$shutdownStatus = $null
$previousAppData = $env:APPDATA
$previousPath = $env:PATH

function Add-Check([string]$Id, [string]$Detail) {
    $checks.Add([ordered]@{ id = $Id; status = "pass"; detail = $Detail })
}

try {
    if (-not (Test-Path -LiteralPath (Join-Path $payloadRoot "deploy\windows\install.ps1"))) {
        throw "Windows release bundle is missing. Run deploy/windows/build-bundle.ps1 first."
    }
    New-Item -ItemType Directory -Force -Path $testRoot, $appDataRoot | Out-Null
    $env:APPDATA = $appDataRoot

    $installScript = Join-Path $payloadRoot "deploy\windows\install.ps1"
    & $installScript -PayloadRoot $payloadRoot -InstallRoot $installRoot -Profile @("nonebot", "koishi")
    $active = Get-Content -LiteralPath (Join-Path $installRoot "active.json") -Raw | ConvertFrom-Json
    $versionRoot = [System.IO.Path]::GetFullPath((Join-Path $installRoot $active.version_path))
    if (@($active.profiles).Count -ne 2 -or @($active.profiles | Where-Object { $_ -notin @("nonebot", "koishi") }).Count -gt 0) {
        throw "The fresh install did not activate both requested profiles."
    }
    Add-Check "fresh_install" "Both locked profiles installed and the candidate version activated from the bundled installer."

    $configureScript = Join-Path $payloadRoot "deploy\windows\configure.ps1"
    & $configureScript -InstallRoot $installRoot -ConfigPath $configPath -UserDataRoot $userDataRoot -Profiles @("nonebot", "koishi")
    $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    if ($config.listen -ne "127.0.0.1:26701" -or $config.allow_non_loopback_listen -ne $false) {
        throw "First-run configuration did not keep Core loopback-only."
    }
    if ($config.admin_token.Length -lt 40 -or @($config.clients | Where-Object { $_.token.Length -lt 40 -or $_.operation_grants.Count -ne 0 }).Count -gt 0) {
        throw "First-run configuration did not create required credentials or least-privilege client grants."
    }
    if (@($config.hosts).Count -ne 2 -or @($config.hosts | Where-Object { $_.operation_grants.Count -ne 0 }).Count -gt 0) {
        throw "First-run configuration did not create two Hosts without operation grants."
    }
    if (-not $config.database_path.StartsWith($userDataRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "The configured database is outside the separate user data directory."
    }
    Add-Check "first_run_configuration" "Generated loopback-only configuration, per-install tokens, separate user data, and no operation grants."

    $pwshPath = (Get-Command pwsh.exe -ErrorAction Stop).Source
    $systemPath = @((Join-Path $env:SystemRoot "System32"), $env:SystemRoot) -join ";"
    $env:PATH = $systemPath
    foreach ($name in @("node", "npm", "python", "python3")) {
        if (Get-Command $name -ErrorAction SilentlyContinue) { throw "System runtime '$name' remained on the Host launch PATH." }
    }

    $runScript = Join-Path $payloadRoot "deploy\windows\run.ps1"
    $argumentLine = '-NoProfile -File "' + $runScript + '" -InstallRoot "' + $installRoot + '" -ConfigPath "' + $configPath + '"'
    $daemonProcess = Start-Process -FilePath $pwshPath -ArgumentList $argumentLine -WorkingDirectory $repoRoot -PassThru `
        -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -WindowStyle Hidden

    $status = $null
    $deadline = [DateTime]::UtcNow.AddMinutes(2)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($daemonProcess.HasExited) {
            $stderrText = if (Test-Path $stderrPath) { Get-Content -LiteralPath $stderrPath -Raw } else { "" }
            throw "Packaged run.ps1 exited before Core became ready: $stderrText"
        }
        try {
            $status = Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:26701/v1/status" -Headers @{ Authorization = ("Bearer " + $config.admin_token) }
            break
        }
        catch { Start-Sleep -Seconds 1 }
    }
    if ($null -eq $status) { throw "Core did not become available at its loopback address within two minutes." }
    $hosts = @($status.hosts)
    if ($hosts.Count -ne 2 -or @($hosts | Where-Object { $_.state -ne "active" -or $_.restart_count -ne 0 -or $null -ne $_.last_error }).Count -gt 0) {
        throw "Core did not report both Hosts active without restarts or errors."
    }
    $hostProcessIds = @($hosts | ForEach-Object { [int]$_.pid })
    foreach ($hostPid in $hostProcessIds) {
        $hostProcess = Get-Process -Id $hostPid -ErrorAction Stop
        if (-not $hostProcess.Path.StartsWith($versionRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Host PID $hostPid did not use a runtime inside the installed version root."
        }
    }
    Add-Check "core_and_hosts" "Packaged Core became ready; both Host processes were active, error-free, and launched from the installed version while system Node/Python were absent from PATH."

    $shutdown = Invoke-WebRequest -Method Post -Uri "http://127.0.0.1:26701/v1/shutdown" -Headers @{ Authorization = ("Bearer " + $config.admin_token) }
    $shutdownStatus = [int]$shutdown.StatusCode
    if ($shutdownStatus -ne 202) { throw "Core shutdown returned HTTP $shutdownStatus instead of 202." }
    if (-not $daemonProcess.WaitForExit(30000)) { throw "The packaged daemon did not exit after graceful shutdown." }
    if ($daemonProcess.ExitCode -ne 0) { throw "The packaged daemon exited with code $($daemonProcess.ExitCode)." }
    foreach ($hostPid in $hostProcessIds) {
        if (Get-Process -Id $hostPid -ErrorAction SilentlyContinue) { throw "Host PID $hostPid remained after graceful shutdown." }
    }
    Add-Check "graceful_shutdown" "Authenticated shutdown returned HTTP 202 and removed Core/Host processes."

    $uninstallScript = Join-Path $payloadRoot "deploy\windows\uninstall.ps1"
    & $uninstallScript -InstallRoot $installRoot -UserDataRoot $userDataRoot
    if (Test-Path -LiteralPath $installRoot) { throw "Uninstall left the application install root behind." }
    if (-not (Test-Path -LiteralPath $configPath) -or -not (Test-Path -LiteralPath $config.database_path)) {
        throw "Default uninstall removed configuration or database user data."
    }
    Add-Check "default_uninstall" "Application versions were removed while daemon configuration and SQLite user data remained."
}
catch {
    $failure = $_.Exception.Message
}
finally {
    if ($null -ne $daemonProcess -and -not $daemonProcess.HasExited) {
        try {
            $configForCleanup = if (Test-Path $configPath) { Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json } else { $null }
            if ($null -ne $configForCleanup) {
                Invoke-WebRequest -Method Post -Uri "http://127.0.0.1:26701/v1/shutdown" -Headers @{ Authorization = ("Bearer " + $configForCleanup.admin_token) } -TimeoutSec 5 | Out-Null
            }
        }
        catch { }
        if (-not $daemonProcess.WaitForExit(10000)) {
            $taskkill = Join-Path $env:SystemRoot "System32\taskkill.exe"
            & $taskkill /PID $daemonProcess.Id /T /F | Out-Null
        }
    }
    $env:APPDATA = $previousAppData
    $env:PATH = $previousPath
}

$reportFull = [System.IO.Path]::GetFullPath($ReportPath)
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $reportFull) | Out-Null
$report = [ordered]@{
    schema_version = 1
    task = "T15"
    status = if ($null -eq $failure) { "pass" } else { "fail" }
    environment = [ordered]@{
        runner_os = if ($env:RUNNER_OS) { $env:RUNNER_OS } else { "local" }
        system_runtime_path_hidden_during_core_start = $true
        app_version = "0.1.0"
        profiles = @("nonebot", "koishi")
    }
    checks = @($checks.ToArray())
    shutdown_http = $shutdownStatus
    failure = $failure
}
$json = $report | ConvertTo-Json -Depth 12
[System.IO.File]::WriteAllText($reportFull, $json + [Environment]::NewLine, (New-Object System.Text.UTF8Encoding($false)))
if ($null -ne $failure) { throw $failure }
Write-Output "Windows package acceptance passed: $reportFull"
