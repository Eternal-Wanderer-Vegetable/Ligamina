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

    $activePath = Join-Path $installRoot "active.json"
    $initialActiveJson = Get-Content -LiteralPath $activePath -Raw
    $initialActive = $initialActiveJson | ConvertFrom-Json
    $initialActiveHash = (Get-FileHash -LiteralPath $activePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $invalidManifest = Get-Content -LiteralPath (Join-Path $payloadRoot "deploy\windows\runtime-manifest.json") -Raw | ConvertFrom-Json
    $invalidManifest.runtimes.uv.sha256 = "0" * 64
    $invalidManifestPath = Join-Path $testRoot "runtime-manifest-invalid-sha256.json"
    [System.IO.File]::WriteAllText($invalidManifestPath, ($invalidManifest | ConvertTo-Json -Depth 16) + [Environment]::NewLine, (New-Object System.Text.UTF8Encoding($false)))
    $updateScript = Join-Path $payloadRoot "deploy\windows\update.ps1"
    $rollbackError = $null
    try {
        & $updateScript -PayloadRoot $payloadRoot -InstallRoot $installRoot -Profile @("nonebot", "koishi") -ManifestPath $invalidManifestPath
        throw "The synthetic update unexpectedly succeeded with an invalid runtime SHA-256."
    }
    catch {
        $rollbackError = $_.Exception.Message
    }
    if ($rollbackError -notmatch "SHA256 mismatch for") {
        throw "The synthetic update failed for an unexpected reason: $rollbackError"
    }
    $activeAfterFailedUpdate = Get-Content -LiteralPath $activePath -Raw | ConvertFrom-Json
    $activeHashAfterFailedUpdate = (Get-FileHash -LiteralPath $activePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $versionsRoot = Join-Path $installRoot "versions"
    $versionDirs = @(Get-ChildItem -LiteralPath $versionsRoot -Directory)
    $originalVersionRoot = Join-Path $installRoot $initialActive.version_path
    if ($activeAfterFailedUpdate.version_id -ne $initialActive.version_id -or
        $activeAfterFailedUpdate.version_path -ne $initialActive.version_path -or
        $activeHashAfterFailedUpdate -ne $initialActiveHash -or
        -not (Test-Path -LiteralPath $originalVersionRoot) -or
        $versionDirs.Count -ne 1) {
        throw "Failed update changed the active version, removed the previous version, or left its failed candidate behind."
    }
    Add-Check "failed_update_rollback" "Rejected a candidate with a corrupted pinned uv archive hash; active.json and the previous version stayed byte-for-byte unchanged and the failed candidate was removed."

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

    $sampleManifest = Get-Content -LiteralPath (Join-Path $repoRoot "samples\third-party.lock.json") -Raw | ConvertFrom-Json
    $sample = $sampleManifest.samples | Where-Object sample_id -eq "nonebot-autohelp"
    if ($null -eq $sample) { throw "The pinned NoneBot autohelp acceptance sample is missing." }
    $pluginModule = $sample.package.Replace("-", "_")
    $wheelName = [System.IO.Path]::GetFileName(([Uri]$sample.artifact_url).AbsolutePath)
    $wheelPath = Join-Path $testRoot $wheelName
    Invoke-WebRequest -UseBasicParsing -Uri $sample.artifact_url -OutFile $wheelPath -TimeoutSec 180
    $wheelHash = (Get-FileHash -LiteralPath $wheelPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($wheelHash -ne $sample.artifact_hash) {
        throw "Third-party wheel SHA-256 mismatch: expected $($sample.artifact_hash), got $wheelHash."
    }

    $runtimeEvidence = Get-Content -LiteralPath (Join-Path $versionRoot "runtime-evidence.json") -Raw | ConvertFrom-Json
    $uvExe = Join-Path (Join-Path $versionRoot "runtimes\uv-$($runtimeEvidence.runtimes.uv.version)") "uv.exe"
    $nonebotPython = Join-Path $versionRoot "hosts\nonebot\.venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $uvExe) -or -not (Test-Path -LiteralPath $nonebotPython)) {
        throw "The bundled uv or NoneBot Python executable is missing from the fresh install."
    }
    $sampleProfileRoot = Join-Path $repoRoot "outputs\acceptance\t13-representative-wsl-final-20261010-locks\nonebot-autohelp"
    if (-not (Test-Path -LiteralPath (Join-Path $sampleProfileRoot "uv.lock"))) {
        throw "The hash-verified T13 NoneBot autohelp lock profile is missing."
    }
    $previousUvProjectEnvironment = $env:UV_PROJECT_ENVIRONMENT
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $versionRoot "hosts\nonebot\.venv"
    try {
        & $uvExe sync --project $sampleProfileRoot --locked --no-dev --no-build --no-progress --python $nonebotPython
        if ($LASTEXITCODE -ne 0) { throw "Installing the locked third-party plugin profile failed with exit code $LASTEXITCODE." }
    }
    finally {
        if ($null -eq $previousUvProjectEnvironment) {
            Remove-Item Env:\UV_PROJECT_ENVIRONMENT -ErrorAction SilentlyContinue
        }
        else {
            $env:UV_PROJECT_ENVIRONMENT = $previousUvProjectEnvironment
        }
    }
    $installedPluginVersion = (& $nonebotPython -c "from importlib.metadata import version; print(version('nonebot-plugin-autohelp'))" | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $installedPluginVersion -ne $sample.version) {
        throw "The installed third-party plugin version was '$installedPluginVersion', expected '$($sample.version)'."
    }

    $nonebotHostConfig = $config.hosts | Where-Object id -eq "nonebot-default" | Select-Object -First 1
    if ($null -eq $nonebotHostConfig) { throw "The fresh configuration is missing the NoneBot Host." }
    $config.gateways[0].account_id = "9000"
    Add-Member -InputObject $nonebotHostConfig.environment -MemberType NoteProperty -Name "LIGAMINA_NONEBOT_PLUGINS" -Value $pluginModule -Force
    Add-Member -InputObject $nonebotHostConfig.environment -MemberType NoteProperty -Name "LIGAMINA_NONEBOT_NICKNAMES" -Value "Bot" -Force
    $hostGrant = [ordered]@{
        id = "nonebot-default-send-grant"
        client_id = $config.mcp_client_id
        account_id = $config.gateways[0].account_id
        operations = @("send_message")
        targets = $null
    }
    $clientGrant = [ordered]@{
        id = "ligamina-local-client-send-grant"
        account_id = $config.gateways[0].account_id
        operations = @("send_message")
        targets = $null
    }
    $nonebotHostConfig.operation_grants = @($hostGrant)
    $config.clients[0].operation_grants = @($clientGrant)
    $encodedConfig = $config | ConvertTo-Json -Depth 16
    [System.IO.File]::WriteAllText($configPath, $encodedConfig + [Environment]::NewLine, (New-Object System.Text.UTF8Encoding($false)))
    Add-Check "third_party_plugin_install" "Installed $($sample.package)==$($sample.version) from the pinned wheel ($wheelHash) into the bundled NoneBot profile; dependencies came from the T13 hash-verified lock profile, and its module was enabled."

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

    $gateway = $config.gateways[0]
    $gatewayHeaders = @{ Authorization = ("Bearer " + $gateway.registration_token) }
    $lease = Invoke-RestMethod -Method Post -Uri ("http://127.0.0.1:26701/v1/gateways/" + [Uri]::EscapeDataString($gateway.id) + "/lease") `
        -Headers $gatewayHeaders -ContentType "application/json" -Body (@{ ttl_ms = 300000 } | ConvertTo-Json -Compress)
    $fixture = Get-Content -LiteralPath (Join-Path $repoRoot "samples\fixtures\third-party\nonebot-autohelp-help-h.json") -Raw | ConvertFrom-Json
    $eventId = "t15-nonebot-autohelp-" + [guid]::NewGuid().ToString("N")
    $event = [ordered]@{
        schema_version = "ligamina/0.1"
        event_id = $eventId
        trace_id = "trace-$eventId"
        platform = "onebot.v11"
        account_id = $gateway.account_id
        conversation = [ordered]@{ kind = "group"; id = [string]$fixture.conversation.id }
        actor_id = [string]$fixture.actor_id
        timestamp = [DateTimeOffset]::UtcNow.ToString("o")
        target_host = "nonebot-default"
        message = @([ordered]@{ type = "text"; text = $fixture.input_texts[0] })
        extensions = [ordered]@{ "onebot.v11" = [ordered]@{ message_id = "80000"; sender = [ordered]@{ nickname = "Alice" } } }
    }
    $clientHeaders = @{ Authorization = ("Bearer " + $config.clients[0].token) }
    Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:26701/v1/events" -Headers $clientHeaders `
        -ContentType "application/json" -Body ($event | ConvertTo-Json -Depth 12 -Compress) | Out-Null

    $operationPayloads = [System.Collections.Generic.List[object]]::new()
    $pluginStatus = $null
    $eventQuery = "platform=onebot.v11&account_id=$([Uri]::EscapeDataString($gateway.account_id))&conversation_kind=group&conversation_id=$([Uri]::EscapeDataString([string]$fixture.conversation.id))"
    $eventStatusUri = "http://127.0.0.1:26701/v1/events/$([Uri]::EscapeDataString($eventId))?$eventQuery"
    $eventDeadline = [DateTime]::UtcNow.AddSeconds(30)
    while ([DateTime]::UtcNow -lt $eventDeadline) {
        $operation = Invoke-RestMethod -Method Post -Uri ("http://127.0.0.1:26701/v1/gateways/" + [Uri]::EscapeDataString($gateway.id) + "/actions/claim") `
            -Headers @{ Authorization = ("Bearer " + $lease.token) } -ContentType "application/json" `
            -Body ($lease | ConvertTo-Json -Depth 8 -Compress)
        if ($null -ne $operation) {
            $params = $operation.operation_payload.params
            $messageParts = [System.Collections.Generic.List[string]]::new()
            if ($params.message -is [string]) {
                $messageParts.Add([string]$params.message)
            }
            else {
                foreach ($segment in @($params.message)) {
                    if ($segment.type -eq "text") {
                        $textProperty = $segment.PSObject.Properties["text"]
                        if ($null -ne $textProperty) { $messageParts.Add([string]$textProperty.Value) }
                        else {
                            $dataProperty = $segment.PSObject.Properties["data"]
                            if ($null -ne $dataProperty) {
                                if ($dataProperty.Value -is [string]) { $messageParts.Add([string]$dataProperty.Value) }
                                else {
                                    $nestedText = $dataProperty.Value.PSObject.Properties["text"]
                                    if ($null -ne $nestedText) { $messageParts.Add([string]$nestedText.Value) }
                                }
                            }
                        }
                    }
                }
            }
            $outgoingText = [string]::Concat($messageParts.ToArray())
            $target = $operation.operation_payload.target
            if ($operation.key.operation -ne "send_message" -or $target.kind -ne "group" -or [string]$target.id -ne [string]$fixture.expected_target.id) {
                throw "The third-party plugin sent an operation to an unexpected type or target."
            }
            if ($outgoingText -cne [string]$fixture.expected_outgoing_texts[0]) {
                throw "The third-party plugin output did not match the pinned L4 fixture."
            }
            $operationPayloads.Add($operation)
            $receiptId = "700001"
            $receipt = [ordered]@{
                key = $operation.key
                fingerprint = $operation.fingerprint
                status = "succeeded"
                platform_message_id = $receiptId
                data = $null
                error = $null
                observed_at_ms = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
                evidence_ref = "ligamina-windows-acceptance://gateway/$receiptId"
            }
            $resultBody = [ordered]@{ credential = $lease; result = $receipt } | ConvertTo-Json -Depth 12 -Compress
            Invoke-RestMethod -Method Post -Uri ("http://127.0.0.1:26701/v1/gateways/" + [Uri]::EscapeDataString($gateway.id) + "/actions/" + [Uri]::EscapeDataString($operation.key.operation_id) + "/result") `
                -Headers @{ Authorization = ("Bearer " + $lease.token) } -ContentType "application/json" -Body $resultBody | Out-Null
        }

        $pluginStatus = Invoke-RestMethod -Method Get -Uri $eventStatusUri -Headers $clientHeaders
        if ($pluginStatus.status -in @("completed", "failed", "unknown")) { break }
        Start-Sleep -Milliseconds 50
    }
    if ($null -eq $pluginStatus -or $pluginStatus.status -ne "completed" -or $operationPayloads.Count -ne 1) {
        throw "The third-party plugin event did not complete through one virtual gateway receipt: $($pluginStatus | ConvertTo-Json -Compress -Depth 6)"
    }
    Add-Check "third_party_plugin_event" "The bundled NoneBot Host ran the pinned autohelp plugin, matched its help response and group target, and received one successful virtual gateway receipt."

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

try {
    $runnerRootFull = [System.IO.Path]::GetFullPath($runnerRoot).TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
    $runnerPrefix = $runnerRootFull + [System.IO.Path]::DirectorySeparatorChar
    $testRootFull = [System.IO.Path]::GetFullPath($testRoot)
    if (-not $testRootFull.StartsWith($runnerPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove an acceptance scratch directory outside the runner temp root: '$testRootFull'."
    }
    if (Test-Path -LiteralPath $testRootFull) { Remove-Item -LiteralPath $testRootFull -Recurse -Force }
}
catch {
    if ($null -eq $failure) { $failure = "Acceptance scratch cleanup failed: $($_.Exception.Message)" }
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
