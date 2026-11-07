param([Parameter(Mandatory = $true)][string] $OutputDirectory)

$ErrorActionPreference = 'Stop'
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$progressPath = Join-Path $OutputDirectory 'guest-progress.jsonl'
$inputManifest = Get-Content -LiteralPath 'C:\P404Input\host-inputs.json' -Raw | ConvertFrom-Json
$sourcePath = 'C:\P404Input\ligamina-cli.exe'
$signTool = 'C:\P404Input\signtool.exe'
$outputCopyDirectory = Join-Path $OutputDirectory 'signed-core'
$signedPath = Join-Path $outputCopyDirectory 'ligamina-cli.exe'
New-Item -ItemType Directory -Path $outputCopyDirectory -Force | Out-Null
$sourceHash = $null
$signedHashBefore = $null
$status = 'probe_failed_or_incomplete'
$cert = $null
$certInfo = $null
$execution = [ordered]@{ started = $false; timed_out = $false; exit_code = $null; stdout = ''; stderr = ''; error = $null; native_error_code = $null }
$verification = [ordered]@{ sign_exit_code = $null; sign_output = ''; verify_exit_code = $null; verify_output = ''; authenticode_status = 'not_checked' }
$enforcement = [ordered]@{
    device_guard_kernel_status = $null
    device_guard_user_mode_status = $null
    baseline_record_ids = @{}
    baseline_current_policy_events = @()
    current_policy_events = @()
    target_file_events = @()
    target_file_activity_ids = @()
    related_target_events = @()
    citool_exit_code = $null
    citool_timed_out = $false
    citool_stdout = ''
    citool_stderr = ''
}
$isolation = [ordered]@{ networking_disabled_by_wsb_profile = $true; mapped_inputs_read_only = $true; guest_trust_stores_changed_temporarily = $false; guest_trust_stores_modified = @(); certificate_cleanup_by_store = [ordered]@{ My = $false; Root = $false; TrustedPublisher = $false }; certificate_cleanup_confirmed = $false; private_key_exported = $false; catalog_plugin_or_behavior_executed = $false }
$probeStart = (Get-Date).ToUniversalTime()

function Write-Stage([string] $Name, [object] $Data = $null) {
    $entry = [ordered]@{ time_utc = (Get-Date).ToUniversalTime().ToString('o'); stage = $Name; data = $Data }
    [System.IO.File]::AppendAllText($progressPath, (($entry | ConvertTo-Json -Depth 8 -Compress) + [Environment]::NewLine), $utf8NoBom)
}

function Add-CurrentUserCertificate([string] $StoreName, [System.Security.Cryptography.X509Certificates.X509Certificate2] $Certificate) {
    $store = New-Object System.Security.Cryptography.X509Certificates.X509Store($StoreName, 'CurrentUser')
    try {
        $store.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
        $store.Add($Certificate)
    }
    finally { $store.Close() }
}

function Remove-CurrentUserCertificate([string] $StoreName, [string] $Thumbprint) {
    $store = New-Object System.Security.Cryptography.X509Certificates.X509Store($StoreName, 'CurrentUser')
    try {
        $store.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
        foreach ($item in @($store.Certificates | Where-Object Thumbprint -eq $Thumbprint)) { $store.Remove($item) }
    }
    finally { $store.Close() }
}

function Get-CurrentUserCertificate([string] $StoreName, [string] $Thumbprint) {
    $store = New-Object System.Security.Cryptography.X509Certificates.X509Store($StoreName, 'CurrentUser')
    try {
        $store.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadOnly)
        return @($store.Certificates | Where-Object Thumbprint -eq $Thumbprint)
    }
    finally { $store.Close() }
}

function Invoke-GuestProcess([string] $Stage, [string] $FilePath, [string] $Arguments, [int] $TimeoutSeconds) {
    $stdoutPath = Join-Path $OutputDirectory ($Stage + '.stdout.txt')
    $stderrPath = Join-Path $OutputDirectory ($Stage + '.stderr.txt')
    Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    Write-Stage ($Stage + '.started') @{ file = $FilePath; timeout_seconds = $TimeoutSeconds }
    $result = [ordered]@{ started = $false; timed_out = $false; exit_code = $null; stdout = ''; stderr = ''; error = $null; native_error_code = $null }
    try {
        $startInfo = New-Object System.Diagnostics.ProcessStartInfo
        $startInfo.FileName = $FilePath
        $startInfo.Arguments = $Arguments
        $startInfo.WorkingDirectory = $OutputDirectory
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        $process = [System.Diagnostics.Process]::Start($startInfo)
        if (-not $process) { throw 'Process.Start returned false' }
        $result.started = $true
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            $result.timed_out = $true
            $process.Kill()
            $process.WaitForExit()
        }
        if ($process.HasExited) { $result.exit_code = [int]$process.ExitCode }
        $result.stdout = [string]$stdoutTask.Result
        $result.stderr = [string]$stderrTask.Result
        $process.Dispose()
    }
    catch {
        $result.error = $_.Exception.Message
        if ($_.Exception.InnerException -is [System.ComponentModel.Win32Exception]) { $result.native_error_code = $_.Exception.InnerException.NativeErrorCode }
        elseif ($_.Exception -is [System.ComponentModel.Win32Exception]) { $result.native_error_code = $_.Exception.NativeErrorCode }
    }
    [System.IO.File]::WriteAllText($stdoutPath, $result.stdout, $utf8NoBom)
    [System.IO.File]::WriteAllText($stderrPath, $result.stderr, $utf8NoBom)
    Write-Stage ($Stage + '.finished') @{ started = $result.started; timed_out = $result.timed_out; exit_code = $result.exit_code; native_error_code = $result.native_error_code; error = $result.error }
    return $result
}

function Get-CodeIntegrityRecords([datetime] $SinceUtc, [hashtable] $Baseline) {
    $records = [System.Collections.Generic.List[object]]::new()
    $errors = [System.Collections.Generic.List[string]]::new()
    foreach ($eventId in @(3033, 3034, 3076, 3077, 3089, 3099)) {
        try {
            $found = Get-WinEvent -FilterHashtable @{ LogName = 'Microsoft-Windows-CodeIntegrity/Operational'; Id = $eventId } -MaxEvents 100 -ErrorAction Stop
            $baselineRecordId = [int64]$Baseline[[string]$eventId]
            foreach ($record in @($found | Where-Object { $_.TimeCreated.ToUniversalTime() -ge $SinceUtc -and $_.RecordId -gt $baselineRecordId })) {
                $records.Add([ordered]@{ id = $record.Id; record_id = $record.RecordId; time_created_utc = $record.TimeCreated.ToUniversalTime().ToString('o'); activity_id = if ($record.ActivityId) { $record.ActivityId.ToString() } else { $null }; message = $record.Message; xml = $record.ToXml() })
            }
        }
        catch {
            if ($_.Exception.Message -notmatch 'No events were found that match the specified selection criteria') {
                $errors.Add("event $eventId query: $($_.Exception.Message)")
            }
        }
    }
    return [ordered]@{ records = @($records); errors = @($errors) }
}

try {
    Write-Stage 'guest_started' @{ user = [Environment]::UserName }
    foreach ($eventId in @(3033, 3034, 3076, 3077, 3089, 3099)) {
        $enforcement.baseline_record_ids[[string]$eventId] = 0
        try {
            $last = Get-WinEvent -FilterHashtable @{ LogName = 'Microsoft-Windows-CodeIntegrity/Operational'; Id = $eventId } -MaxEvents 1 -ErrorAction Stop
            $enforcement.baseline_record_ids[[string]$eventId] = [int64]$last.RecordId
        }
        catch {}
    }
    try {
        $policyBaseline = Get-WinEvent -FilterHashtable @{ LogName = 'Microsoft-Windows-CodeIntegrity/Operational'; Id = 3099 } -MaxEvents 20 -ErrorAction Stop
        $enforcement.baseline_current_policy_events = @($policyBaseline | ForEach-Object {
            [ordered]@{ id = $_.Id; record_id = $_.RecordId; time_created_utc = $_.TimeCreated.ToUniversalTime().ToString('o'); message = $_.Message; xml = $_.ToXml() }
        })
    }
    catch { $enforcement.baseline_policy_event_query_error = $_.Exception.Message }
    Write-Stage 'baseline_events_collected' @{ record_ids = $enforcement.baseline_record_ids; current_policy_events = $enforcement.baseline_current_policy_events.Count }
    if (-not (Test-Path -LiteralPath $sourcePath) -or -not (Test-Path -LiteralPath $signTool)) { throw 'Mapped Core or SignTool input is missing' }
    Copy-Item -LiteralPath $sourcePath -Destination $signedPath
    $sourceHash = (Get-FileHash -LiteralPath $sourcePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $signedHashBefore = (Get-FileHash -LiteralPath $signedPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($sourceHash -ne $inputManifest.core_sha256.ToLowerInvariant()) { throw 'Read-only Core input hash mismatch' }
    if ($signedHashBefore -ne $sourceHash) { throw 'Copied Core hash differs before signing' }
    Write-Stage 'input_hashes_verified' @{ source_sha256 = $sourceHash }

    $cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject 'CN=Ligamina P4-04 Disposable Self-Signed Core Test' -CertStoreLocation 'Cert:\CurrentUser\My' -KeyAlgorithm RSA -KeyLength 2048 -HashAlgorithm SHA256 -KeyExportPolicy NonExportable -NotAfter (Get-Date).AddHours(12)
    $certInfo = [ordered]@{ subject = $cert.Subject; issuer = $cert.Issuer; thumbprint = $cert.Thumbprint; private_key_exportable = $false; private_key_exported = $false }
    Write-Stage 'certificate_created' @{ thumbprint = $cert.Thumbprint; private_key_exported = $false }
    foreach ($storeName in @('Root', 'TrustedPublisher')) {
        Add-CurrentUserCertificate $storeName $cert
        $isolation.guest_trust_stores_modified += $storeName
        $isolation.guest_trust_stores_changed_temporarily = $true
        Write-Stage 'guest_trust_store_modified' @{ store = $storeName; thumbprint = $cert.Thumbprint; scope = 'CurrentUser' }
    }
    $signArguments = 'sign /fd SHA256 /sha1 ' + $cert.Thumbprint + ' /s My /v ' + $signedPath
    $signResult = Invoke-GuestProcess -Stage 'signtool-sign' -FilePath $signTool -Arguments $signArguments -TimeoutSeconds 90
    $verification.sign_exit_code = $signResult.exit_code
    $verification.sign_output = ($signResult.stdout + $signResult.stderr).Trim()
    if ($signResult.timed_out -or -not $signResult.started -or $signResult.exit_code -ne 0) { throw "SignTool signing did not complete successfully: $($signResult.error)" }
    Write-Stage 'signature_written' @{ signed_sha256 = (Get-FileHash -LiteralPath $signedPath -Algorithm SHA256).Hash.ToLowerInvariant() }

    $verifyArguments = 'verify /pa /v ' + $signedPath
    $verifyResult = Invoke-GuestProcess -Stage 'signtool-verify' -FilePath $signTool -Arguments $verifyArguments -TimeoutSeconds 90
    $verification.verify_exit_code = $verifyResult.exit_code
    $verification.verify_output = ($verifyResult.stdout + $verifyResult.stderr).Trim()
    $verification.authenticode_status = if ($verifyResult.exit_code -eq 0 -and -not $verifyResult.timed_out) { 'SignTool_Verified_With_GuestOnly_Root_And_TrustedPublisher' } else { 'SignTool_Not_Verified_With_GuestOnly_Root_And_TrustedPublisher' }
    Write-Stage 'signature_verification_finished' @{ started = $verifyResult.started; timed_out = $verifyResult.timed_out; exit_code = $verifyResult.exit_code }
    if (-not $verifyResult.started) { throw 'SignTool verification could not be started; Core execution withheld' }

    $coreResult = Invoke-GuestProcess -Stage 'core-version' -FilePath $signedPath -Arguments '--version' -TimeoutSeconds 30
    $execution.started = $coreResult.started
    $execution.timed_out = $coreResult.timed_out
    $execution.exit_code = $coreResult.exit_code
    $execution.stdout = $coreResult.stdout
    $execution.stderr = $coreResult.stderr
    $execution.error = $coreResult.error
    $execution.native_error_code = $coreResult.native_error_code
    Write-Stage 'core_version_finished' @{ started = $execution.started; timed_out = $execution.timed_out; exit_code = $execution.exit_code; error = $execution.error }

    Write-Stage 'event_collection_started'
    $eventResult = Get-CodeIntegrityRecords -SinceUtc $probeStart -Baseline $enforcement.baseline_record_ids
    $enforcement.current_policy_events = @($eventResult.records | Where-Object id -eq 3099)
    $leaf = [regex]::Escape([System.IO.Path]::GetFileName($signedPath))
    $enforcement.target_file_events = @($eventResult.records | Where-Object { $_.message -match $leaf -or $_.xml -match $leaf })
    $enforcement.target_file_activity_ids = @($enforcement.target_file_events | Where-Object activity_id | Select-Object -ExpandProperty activity_id -Unique)
    if ($enforcement.target_file_activity_ids.Count -gt 0) {
        $enforcement.related_target_events = @($eventResult.records | Where-Object { $_.activity_id -and $enforcement.target_file_activity_ids -contains $_.activity_id })
    }
    $enforcement.event_query_errors = $eventResult.errors
    Write-Stage 'event_collection_finished' @{ records = $eventResult.records.Count; errors = $eventResult.errors.Count }

    $ci = Invoke-GuestProcess -Stage 'citool-list' -FilePath (Join-Path $env:WINDIR 'System32\CiTool.exe') -Arguments '-lp -json' -TimeoutSeconds 30
    $enforcement.citool_stdout = $ci.stdout
    $enforcement.citool_stderr = $ci.stderr
    $enforcement.citool_exit_code = $ci.exit_code
    $enforcement.citool_timed_out = $ci.timed_out
    try {
        Write-Stage 'device_guard_query_started'
        $guard = Get-CimInstance -Namespace 'root\Microsoft\Windows\DeviceGuard' -ClassName Win32_DeviceGuard
        $enforcement.device_guard_kernel_status = $guard.CodeIntegrityPolicyEnforcementStatus
        $enforcement.device_guard_user_mode_status = $guard.UserModeCodeIntegrityPolicyEnforcementStatus
        Write-Stage 'device_guard_query_finished'
    }
    catch { $enforcement.device_guard_error = $_.Exception.Message }

    if ($enforcement.target_file_events | Where-Object id -eq 3077) { $status = 'blocked_by_enforcement_event_correlated' }
    elseif ($execution.native_error_code -eq 4551 -or $execution.error -match 'An Application Control policy has blocked this file') { $status = 'blocked_by_enforcement_win32_4551_event_attribution_incomplete' }
    elseif ($execution.started -and $execution.exit_code -eq 0 -and -not $execution.timed_out) { $status = 'passed_runtime_only' }
    else { $status = 'unknown_policy_or_execution_result' }
}
catch {
    $execution.error = if ($execution.error) { $execution.error + '; ' + $_.Exception.Message } else { $_.Exception.Message }
    Write-Stage 'probe_error' @{ message = $_.Exception.Message }
}
finally {
    Write-Stage 'certificate_cleanup_started' @{ has_certificate = [bool]$cert; trust_stores_modified = $isolation.guest_trust_stores_modified }
    if ($cert) {
        foreach ($storeName in @('My', 'Root', 'TrustedPublisher')) {
            try {
                Remove-CurrentUserCertificate $storeName $cert.Thumbprint
                $remaining = @(Get-CurrentUserCertificate $storeName $cert.Thumbprint)
                $isolation.certificate_cleanup_by_store[$storeName] = ($remaining.Count -eq 0)
                if ($remaining.Count -ne 0) { throw "Certificate remains in CurrentUser\$storeName" }
            }
            catch { $isolation.certificate_cleanup_error = "$storeName`: $($_.Exception.Message)" }
        }
    }
    $isolation.certificate_cleanup_confirmed = (@($isolation.certificate_cleanup_by_store.Values | Where-Object { -not $_ }).Count -eq 0 -and -not $isolation.Contains('certificate_cleanup_error'))
    Write-Stage 'certificate_cleanup_finished' @{ confirmed = $isolation.certificate_cleanup_confirmed; by_store = $isolation.certificate_cleanup_by_store }
    $signedHash = if (Test-Path -LiteralPath $signedPath) { (Get-FileHash -LiteralPath $signedPath -Algorithm SHA256).Hash.ToLowerInvariant() } else { $null }
    $report = [ordered]@{
        schema_version = 1
        kind = 'phase4_p404_selfsigned_core_probe'
        collected_at_utc = (Get-Date).ToUniversalTime().ToString('o')
        status = $status
        source_core = [ordered]@{ filename = [System.IO.Path]::GetFileName($sourcePath); source_sha256 = $sourceHash; signed_copy_sha256 = $signedHash; copied_source_sha256 = $signedHashBefore; host_locked_sha256 = $inputManifest.core_sha256 }
        certificate = $certInfo
        signature_verification = $verification
        execution = $execution
        enforcement = $enforcement
        isolation = $isolation
        limitation = 'A local release executable signed with a guest-only self-issued certificate is an experimental derivative. --version runtime execution is not Core/Host federated behavior, catalog admission, or release readiness.'
    }
    Write-Stage 'report_write_started' @{ status = $status }
    [System.IO.File]::WriteAllText((Join-Path $OutputDirectory 'selfsigned-core-probe.json'), ($report | ConvertTo-Json -Depth 20) + [Environment]::NewLine, $utf8NoBom)
    Write-Stage 'report_written' @{ status = $status }
}
