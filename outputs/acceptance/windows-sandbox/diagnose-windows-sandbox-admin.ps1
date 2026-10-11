$ErrorActionPreference = "Stop"
$reportPath = Join-Path $PSScriptRoot "sandbox-host-diagnostics-20261010.json"
$feature = Get-WindowsOptionalFeature -Online -FeatureName "Containers-DisposableClientVM"
$events = @()
foreach ($logName in @("Microsoft-Windows-Hyper-V-Compute-Admin", "Microsoft-Windows-Hyper-V-Compute-Operational", "System", "Application")) {
    try {
        $logEvents = Get-WinEvent -FilterHashtable @{ LogName = $logName; StartTime = [DateTime]::Now.AddHours(-2) } -MaxEvents 100 -ErrorAction Stop
        foreach ($event in $logEvents) {
            if ($logName -in @("System", "Application") -and $event.ProviderName -notmatch "Sandbox|Hyper-V|Host Compute|vmcompute|vmwp|Windows Error Reporting|Application Error") { continue }
            $events += [ordered]@{
                log_name = $logName
                provider = $event.ProviderName
                time_created = $event.TimeCreated.ToUniversalTime().ToString("o")
                id = $event.Id
                level = $event.LevelDisplayName
                message = $event.Message
            }
        }
    } catch {
        $events += [ordered]@{ log_name = $logName; read_error = $_.Exception.Message }
    }
}
$report = [ordered]@{
    schema_version = 1
    captured_at_utc = [DateTime]::UtcNow.ToString("o")
    feature_name = $feature.FeatureName
    feature_state = [string]$feature.State
    sandbox_executable_present = Test-Path (Join-Path $env:WINDIR "System32\WindowsSandbox.exe")
    windows_sandbox_processes = @(Get-Process -Name WindowsSandbox -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
    hyperv_compute_status = (Get-Service -Name vmcompute).Status.ToString()
    relevant_events = @($events)
}
$json = $report | ConvertTo-Json -Depth 10
$tempPath = "$reportPath.tmp"
[System.IO.File]::WriteAllText($tempPath, $json + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
$stream = [System.IO.File]::Open($tempPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
try { $stream.Flush($true) } finally { $stream.Dispose() }
Move-Item -LiteralPath $tempPath -Destination $reportPath -Force
Write-Output "Saved Windows Sandbox host diagnostics: $reportPath"
