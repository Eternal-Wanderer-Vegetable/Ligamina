$ErrorActionPreference = "Stop"
$featureName = "Containers-DisposableClientVM"
$reportPath = Join-Path $PSScriptRoot "sandbox-feature-install-20261010.json"
$report = [ordered]@{
    schema_version = 1
    task = "C14"
    feature_name = $featureName
    started_at_utc = [DateTime]::UtcNow.ToString("o")
    result = "running"
    feature_state = $null
    restart_needed = $false
    reboot_issued = $false
    failure = $null
}

function Save-FeatureReport {
    $json = $report | ConvertTo-Json -Depth 8
    [System.IO.File]::WriteAllText($reportPath, $json + [Environment]::NewLine, (New-Object System.Text.UTF8Encoding($false)))
}

try {
    $enableResult = Enable-WindowsOptionalFeature -Online -FeatureName $featureName -All -NoRestart
    $feature = Get-WindowsOptionalFeature -Online -FeatureName $featureName
    $report.feature_state = [string]$feature.State
    $report.restart_needed = [bool]$enableResult.RestartNeeded
    if ($feature.State -ne "Enabled") {
        throw "Windows Sandbox feature state is '$($feature.State)' after enablement."
    }

    $report.result = "enabled"
    $report.reboot_issued = $true
    Save-FeatureReport
    Restart-Computer -Force
}
catch {
    $report.result = "failed"
    $report.failure = $_.Exception.Message
    try { Save-FeatureReport } catch { }
    throw
}
