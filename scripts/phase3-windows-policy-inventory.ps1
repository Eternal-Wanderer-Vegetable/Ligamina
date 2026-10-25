[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory
)

$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null

$os = Get-CimInstance -ClassName Win32_OperatingSystem |
    Select-Object Caption, Version, BuildNumber, OSArchitecture

$ciToolPath = Join-Path $env:windir 'System32\CiTool.exe'
$ciToolOutput = @(& $ciToolPath -lp -json 2>&1)
$ciToolExitCode = $LASTEXITCODE
$ciToolText = ($ciToolOutput | Out-String).Trim()
$ciToolJson = $null
$ciToolParseError = $null
try {
    $ciToolJson = $ciToolText | ConvertFrom-Json -ErrorAction Stop
}
catch {
    $ciToolParseError = $_.Exception.Message
}

$eventLog = 'Microsoft-Windows-CodeIntegrity/Operational'
$events = @(
    Get-WinEvent -FilterHashtable @{
        LogName = $eventLog
        Id = @(3076, 3077, 3089, 3099)
    } -ErrorAction SilentlyContinue |
        ForEach-Object {
            [ordered]@{
                event_id = $_.Id
                record_id = $_.RecordId
                time_created = $_.TimeCreated.ToUniversalTime().ToString('o')
                activity_id = [string]$_.ActivityId
                provider = $_.ProviderName
                message = $_.Message
            }
        }
)

$evtxPath = Join-Path $OutputDirectory 'CodeIntegrity-Operational.evtx'
& (Join-Path $env:windir 'System32\wevtutil.exe') epl $eventLog $evtxPath '/ow:true'
$evtxExportExitCode = $LASTEXITCODE

$eventCounts = [ordered]@{}
foreach ($eventId in @(3076, 3077, 3089, 3099)) {
    $eventCounts[[string]$eventId] = @($events | Where-Object event_id -eq $eventId).Count
}

$report = [ordered]@{
    schema_version = 1
    task_id = 'p3-p305-windows-policy-inventory'
    collected_at = [DateTimeOffset]::Now.ToString('o')
    guest_os = $os
    ci_tool = [ordered]@{
        command = 'CiTool.exe -lp -json'
        exit_code = $ciToolExitCode
        parsed_json = $ciToolJson
        stdout = $ciToolText
        parse_error = $ciToolParseError
    }
    codeintegrity = [ordered]@{
        event_log = $eventLog
        requested_event_ids = @(3076, 3077, 3089, 3099)
        event_counts = $eventCounts
        events = $events
        evtx_file = [IO.Path]::GetFileName($evtxPath)
        evtx_export_exit_code = $evtxExportExitCode
    }
}

$jsonPath = Join-Path $OutputDirectory 'guest-policy-inventory.json'
$report | ConvertTo-Json -Depth 16 |
    Set-Content -LiteralPath $jsonPath -Encoding UTF8

Write-Output "report=$jsonPath"
Write-Output "evtx=$evtxPath"
Write-Output "citool_exit=$ciToolExitCode; evtx_exit=$evtxExportExitCode"
