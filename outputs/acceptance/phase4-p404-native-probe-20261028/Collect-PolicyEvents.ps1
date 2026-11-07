$ErrorActionPreference='Continue'
$startLocal=(Get-Date).AddHours(-2)
$logs=@(Get-WinEvent -ListLog '*CodeIntegrity*','*AppLocker*' -ErrorAction SilentlyContinue | ForEach-Object { [ordered]@{ log_name=$_.LogName; enabled=$_.IsEnabled; record_count=$_.RecordCount; last_write_time=$_.LastWriteTime.ToUniversalTime().ToString('o') } })
$events=@()
$errors=@()
foreach($logName in @('Microsoft-Windows-CodeIntegrity/Operational','Microsoft-Windows-AppLocker/EXE and DLL','Microsoft-Windows-AppLocker/MSI and Script')) {
  try { $events += @(Get-WinEvent -FilterHashtable @{ LogName=$logName; StartTime=$startLocal } -ErrorAction Stop | ForEach-Object { [ordered]@{ log_name=$logName; time_created_utc=$_.TimeCreated.ToUniversalTime().ToString('o'); id=$_.Id; record_id=$_.RecordId; provider=$_.ProviderName; activity_id=$_.ActivityId.ToString(); message=$_.Message; event_xml=$_.ToXml() } }) }
  catch { $errors += [ordered]@{log_name=$logName; error=$_.Exception.Message} }
}
$report=[ordered]@{schema_version=1;kind='phase4_p404_native_probe_event_followup';collected_at_utc=[DateTime]::UtcNow.ToString('o');window_start_local=$startLocal.ToString('o');logs=$logs;events=$events;errors=$errors}
$path='C:\P404Results\native-probe-event-followup.json'
$json=$report|ConvertTo-Json -Depth 12
[IO.File]::WriteAllText($path,$json+[Environment]::NewLine,(New-Object Text.UTF8Encoding($false)))
$hash=(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
[IO.File]::WriteAllText(($path+'.sha256'),($hash+'  native-probe-event-followup.json'+[Environment]::NewLine),(New-Object Text.UTF8Encoding($false)))
