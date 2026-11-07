$ErrorActionPreference = 'Stop'
$root = 'outputs/acceptance'
$guest = Get-Content "$root/phase4-p404-guest-admission-20261028-v2/guest-admission.json" -Raw | ConvertFrom-Json
$policy = Get-Content "$root/phase4-p404-guest-admission-review-20261028-v2.json" -Raw | ConvertFrom-Json
$probePath = "$root/phase4-p404-guest-admission-20261028-v2/native-probe-report.json"
$eventsPath = "$root/phase4-p404-guest-admission-20261028-v2/native-probe-event-correlation.json"
$probe = Get-Content $probePath -Raw | ConvertFrom-Json
$events = Get-Content $eventsPath -Raw | ConvertFrom-Json
$blocking = @($events.events | Where-Object id -eq 3077)
if ($blocking.Count -ne 1) { throw "expected exactly one 3077, got $($blocking.Count)" }
$activity = $blocking[0].activity_id
$related = @($events.events | Where-Object { $_.activity_id -eq $activity -and $_.id -in @(3077,3089) })
$xml = [xml]$blocking[0].event_xml
$data = @{}
foreach ($item in $xml.Event.EventData.Data) { $data[$item.Name] = $item.'#text' }
if ($data['SHA256 Flat Hash'] -ne $probe.probe.sha256) { throw 'CI event flat hash does not match probe file hash' }
$policyMatch = @($policy.code_integrity_events.loaded_policy_summaries | Where-Object policy_guid -eq $data['PolicyGUID'])
if ($policyMatch.Count -ne 1) { throw 'blocking policy missing from 3099 review' }
$files = @(
  $probePath,
  "$probePath.sha256",
  $eventsPath,
  "$eventsPath.sha256",
  "$root/phase4-p404-guest-admission-review-20261028-v2.json",
  "$root/phase4-p404-guest-admission-review-20261028-v2.json.sha256",
  "$root/phase4-p404-native-probe-20261028/phase4-p404-native-probe.exe"
)
$sourceHashes = @()
foreach ($file in $files) {
  $hash = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant()
  $sourceHashes += [ordered]@{ path = $file.Replace('\','/'); sha256 = $hash }
}
$signatureEvents = @($related | Where-Object id -eq 3089 | ForEach-Object {
  [ordered]@{ event_id = $_.id; record_id = $_.record_id; time_created_utc = $_.time_created_utc; activity_id = $_.activity_id; event_xml = $_.event_xml }
})
$review = [ordered]@{
  schema_version = 1
  kind = 'phase4_p404_native_probe_policy_review'
  reviewed_at_utc = [DateTime]::UtcNow.ToString('o')
  guest = [ordered]@{
    computer_name = $guest.guest.computer_name
    os = $guest.guest.os_caption
    version = $guest.guest.version
    build = $guest.guest.build
    architecture = $guest.guest.architecture
    identity = $guest.guest.current_identity
    kernel_code_integrity_status = $guest.win32_device_guard.code_integrity_policy_enforcement_status
    user_mode_code_integrity_status = $guest.win32_device_guard.usermode_code_integrity_policy_enforcement_status
  }
  probe = [ordered]@{
    path = 'C:\P404NativeProbe\phase4-p404-native-probe.exe'
    sha256 = $probe.probe.sha256
    signature_status = $probe.probe.signature_status
    launch_started = $probe.probe.launch_started
    exit_code = $probe.probe.exit_code
    launch_error = $probe.probe.launch_error
    marker_created = $probe.probe.marker_present
    process_exited = $probe.cleanup.process_exited
  }
  block_event = [ordered]@{
    event_id = $blocking[0].id
    record_id = $blocking[0].record_id
    time_created_utc = $blocking[0].time_created_utc
    activity_id = $activity
    file_name = $data['File Name']
    process_name = $data['Process Name']
    status = $data['Status']
    sha1 = $data['SHA1 Hash']
    sha256_authenticode = $data['SHA256 Hash']
    sha256_flat = $data['SHA256 Flat Hash']
    requested_signing_level = $data['Requested Signing Level']
    validated_signing_level = $data['Validated Signing Level']
    policy_name = $data['PolicyName']
    policy_id = $data['PolicyID']
    policy_guid = $data['PolicyGUID']
    policy_hash = $data['PolicyHash']
    related_signature_events = $signatureEvents
    event_xml = $blocking[0].event_xml
  }
  active_policy = [ordered]@{
    name = $policyMatch[0].policy_name
    policy_id = $policyMatch[0].policy_id
    guid = $policyMatch[0].policy_guid
    options = $policyMatch[0].options
    audit_mode_option_bit16 = $policyMatch[0].audit_mode_option_bit16
    event_derived_mode = $policyMatch[0].event_derived_mode
    activation_status = $policyMatch[0].activation_status
    policy_hash = $policyMatch[0].policy_hash
  }
  citool = [ordered]@{
    exit_code = $guest.citool_list_policies.exit_code
    stdout = $guest.citool_list_policies.stdout
    error_hresult = $policy.citool.hresult
    inventory_verified = $false
  }
  assessment = [ordered]@{
    native_probe = 'blocked_by_enforced_app_control_policy'
    block_is_correlated = $true
    policy_mode = 'enforcement_by_3099_options_and_3077'
    admission = 'not_evaluated'
    catalog_plugin_claims_allowed = $false
    catalog_execution = 'not_started'
    next_gate = 'prepare admin-reviewed Sandbox-only signed policy or trusted-binary package; do not deploy to host; rerun this exact self-owned probe in a fresh guest after explicit review'
  }
  correlated_event_count = $related.Count
  source_files = $sourceHashes
  boundary = 'The unsigned self-owned PE did not enter main. The 3077 event binds its flat SHA-256 to an enforced policy. No catalog plugin or host policy mutation occurred.'
}
$out = Join-Path $root 'phase4-p404-native-probe-review-20261028.json'
$json = $review | ConvertTo-Json -Depth 16
[IO.File]::WriteAllText((Join-Path (Resolve-Path $root).Path 'phase4-p404-native-probe-review-20261028.json'), $json + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
$sha = (Get-FileHash $out -Algorithm SHA256).Hash.ToLowerInvariant()
[IO.File]::WriteAllText(($out + '.sha256'), ($sha + '  phase4-p404-native-probe-review-20261028.json' + [Environment]::NewLine), (New-Object Text.UTF8Encoding($false)))
Write-Output "review_sha256=$sha"
[pscustomobject]@{ event = $review.block_event.event_id; activity_id = $activity; policy = $review.active_policy.name; options = $review.active_policy.options; flat_hash_matches = ($review.block_event.sha256_flat -eq $review.probe.sha256); related_3089 = $signatureEvents.Count; status = $review.block_event.status; admission = $review.assessment.admission; catalog = $review.assessment.catalog_plugin_claims_allowed } | Format-List
