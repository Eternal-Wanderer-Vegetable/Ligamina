$ErrorActionPreference='Continue'
$startUtc=[DateTime]::Parse('2026-10-27T16:21:09.0000000Z').ToUniversalTime()
$startLocal=$startUtc.ToLocalTime()
function Convert-Event($event,$logName) {
  $message=$null; $messageError=$null; $activity=$null; $xml=$null; $xmlError=$null
  try { $message=$event.Message } catch { $messageError=$_.Exception.Message }
  try { if($event.ActivityId) { $activity=[string]$event.ActivityId } } catch {}
  try { $xml=$event.ToXml() } catch { $xmlError=$_.Exception.Message }
  [ordered]@{log_name=$logName;time_created_utc=$event.TimeCreated.ToUniversalTime().ToString('o');id=$event.Id;record_id=$event.RecordId;provider=$event.ProviderName;activity_id=$activity;message=$message;message_error=$messageError;event_xml=$xml;xml_error=$xmlError}
}
$logs=@(Get-WinEvent -ListLog '*CodeIntegrity*','*AppLocker*' -ErrorAction SilentlyContinue|ForEach-Object{[ordered]@{log_name=$_.LogName;enabled=$_.IsEnabled;record_count=$_.RecordCount;last_write_time=$_.LastWriteTime.ToUniversalTime().ToString('o')}})
$events=@();$errors=@()
foreach($logName in @('Microsoft-Windows-CodeIntegrity/Operational','Microsoft-Windows-AppLocker/EXE and DLL','Microsoft-Windows-AppLocker/MSI and Script')){
 try { $rows=@(Get-WinEvent -FilterHashtable @{LogName=$logName;StartTime=$startLocal} -MaxEvents 100 -ErrorAction Stop); foreach($row in $rows){$events+=Convert-Event $row $logName} }
 catch { $errors += [ordered]@{log_name=$logName;error=$_.Exception.Message} }
}
$report=[ordered]@{schema_version=1;kind='phase4_p404_native_probe_event_correlation';collected_at_utc=[DateTime]::UtcNow.ToString('o');window_start_utc=$startUtc.ToString('o');logs=$logs;events=$events;errors=$errors}
$path='C:\P404Results\native-probe-event-correlation.json';$json=$report|ConvertTo-Json -Depth 10;[IO.File]::WriteAllText($path,$json+[Environment]::NewLine,(New-Object Text.UTF8Encoding($false)));$hash=(Get-FileHash $path -Algorithm SHA256).Hash.ToLowerInvariant();[IO.File]::WriteAllText(($path+'.sha256'),($hash+'  native-probe-event-correlation.json'+[Environment]::NewLine),(New-Object Text.UTF8Encoding($false)))
