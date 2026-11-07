$ErrorActionPreference = 'Stop'
$source = 'C:\ProgramData\Ligamina\InternalOutput'
$destination = 'C:\ProgramData\Ligamina\Outbound'
foreach ($name in @('result.json', 'stdout.bin', 'stderr.bin')) {
  $sourcePath = Join-Path $source $name
  if (Test-Path -LiteralPath $sourcePath -PathType Leaf) {
    Copy-Item -LiteralPath $sourcePath -Destination (Join-Path $destination $name) -Force
  }
}

$windowStart = (Get-Date).ToUniversalTime().AddHours(-2)
$events = @()
$queryError = $null
try {
  $records = Get-WinEvent -FilterHashtable @{
    LogName = 'Microsoft-Windows-CodeIntegrity/Operational'
    StartTime = $windowStart.ToLocalTime()
    Id = @(3076, 3077, 3089, 3099)
  } -ErrorAction Stop
  foreach ($record in $records) {
    $events += [pscustomobject]@{
      time_created_utc = $record.TimeCreated.ToUniversalTime().ToString('o')
      id = $record.Id
      record_id = $record.RecordId
      activity_id = if ($record.ActivityId) { $record.ActivityId.ToString() } else { $null }
      provider = $record.ProviderName
      message = $record.Message
      xml = $record.ToXml()
    }
  }
} catch {
  if ($_.FullyQualifiedErrorId -notmatch 'NoMatchingEventsFound') {
    $queryError = $_.Exception.Message
  }
}
$document = [pscustomobject]@{
  schema_version = 1
  kind = 'phase4_p404_koishi_codeintegrity_window'
  collected_at_utc = (Get-Date).ToUniversalTime().ToString('o')
  window_start_utc = $windowStart.ToString('o')
  event_count = $events.Count
  query_error = $queryError
  events = $events
}
$line = 'LIGAMINA_CODEINTEGRITY=' + ($document | ConvertTo-Json -Depth 8 -Compress)
$stdoutPath = Join-Path $destination 'stdout.bin'
if (-not (Test-Path -LiteralPath $stdoutPath -PathType Leaf)) {
  throw 'The trusted worker did not produce stdout for the Code Integrity evidence append.'
}
[System.IO.File]::AppendAllText($stdoutPath, [Environment]::NewLine + $line + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
