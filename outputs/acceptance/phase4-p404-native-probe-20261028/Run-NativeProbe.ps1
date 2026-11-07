$ErrorActionPreference = 'Stop'
$probePath = 'C:\P404NativeProbe\phase4-p404-native-probe.exe'
$markerPath = 'C:\P404Results\native-probe-marker.txt'
$reportPath = 'C:\P404Results\native-probe-report.json'
if (Test-Path -LiteralPath $markerPath) { Remove-Item -LiteralPath $markerPath -Force }
$startUtc = [DateTime]::UtcNow
$probeHash = (Get-FileHash -LiteralPath $probePath -Algorithm SHA256).Hash
$signature = Get-AuthenticodeSignature -LiteralPath $probePath
$launch = [ordered]@{ started = $false; exit_code = $null; error = $null }
$proc = New-Object System.Diagnostics.Process
$proc.StartInfo = New-Object System.Diagnostics.ProcessStartInfo
$proc.StartInfo.FileName = $probePath
$proc.StartInfo.UseShellExecute = $false
try {
    $launch.started = $proc.Start()
    if ($launch.started) {
        $proc.WaitForExit()
        $launch.exit_code = $proc.ExitCode
    }
} catch {
    $launch.error = $_.Exception.ToString()
}
$endUtc = [DateTime]::UtcNow
$eventResults = @()
$eventError = $null
try {
    $eventResults = @(Get-WinEvent -FilterHashtable @{ LogName='Microsoft-Windows-CodeIntegrity/Operational'; Id=@(3076,3077,3089,3099); StartTime=$startUtc.ToLocalTime() } -ErrorAction Stop | ForEach-Object {
        [ordered]@{
            time_created_utc = $_.TimeCreated.ToUniversalTime().ToString('o')
            id = $_.Id
            record_id = $_.RecordId
            provider = $_.ProviderName
            activity_id = $_.ActivityId.ToString()
            message = $_.Message
            event_xml = $_.ToXml()
        }
    })
} catch { $eventError = $_.Exception.Message }
$markerPresent = Test-Path -LiteralPath $markerPath
$markerText = if ($markerPresent) { Get-Content -LiteralPath $markerPath -Raw } else { $null }
$report = [ordered]@{
    schema_version = 1
    kind = 'phase4_p404_self_owned_native_probe'
    guest = [ordered]@{ computer_name=$env:COMPUTERNAME; os_version=[Environment]::OSVersion.Version.ToString(); architecture=[Environment]::GetEnvironmentVariable('PROCESSOR_ARCHITECTURE'); identity=[Security.Principal.WindowsIdentity]::GetCurrent().Name }
    probe = [ordered]@{ path=$probePath; sha256=$probeHash; signature_status=[string]$signature.Status; file_version=(Get-Item -LiteralPath $probePath).VersionInfo.FileVersion; start_utc=$startUtc.ToString('o'); end_utc=$endUtc.ToString('o'); launch_started=$launch.started; exit_code=$launch.exit_code; launch_error=$launch.error; marker_path=$markerPath; marker_present=$markerPresent; marker_text=$markerText }
    code_integrity = [ordered]@{ log='Microsoft-Windows-CodeIntegrity/Operational'; event_ids=@(3076,3077,3089,3099); events=$eventResults; collection_error=$eventError }
    cleanup = [ordered]@{ process_exited=(!$launch.started -or $proc.HasExited); descendant_processes_launched=$false; probe_binary_removed=$false }
    boundary = 'Self-owned native executable only; no catalog plugin, network, policy mutation, interpreter launch, or process-tree creation.'
}
$json = $report | ConvertTo-Json -Depth 12
[IO.File]::WriteAllText($reportPath, $json + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
$hash = (Get-FileHash -LiteralPath $reportPath -Algorithm SHA256).Hash.ToLowerInvariant()
[IO.File]::WriteAllText(($reportPath + '.sha256'), ($hash + '  native-probe-report.json' + [Environment]::NewLine), (New-Object Text.UTF8Encoding($false)))
