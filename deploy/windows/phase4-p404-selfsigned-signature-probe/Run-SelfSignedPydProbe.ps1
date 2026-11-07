param(
    [Parameter(Mandatory = $true)]
    [string] $OutputDirectory
)

$ErrorActionPreference = 'Stop'
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$probeStart = (Get-Date).ToUniversalTime()
$sourcePath = 'C:\P404Input\pyd\_pydantic_core.cp312-win_amd64.pyd'
$pythonPath = 'C:\P404Input\runtime\python.exe'
$signToolPath = 'C:\P404Input\tools\signtool.exe'
$candidateDirectory = Join-Path $env:TEMP 'LigaminaP404SelfSigned'
$candidatePath = Join-Path $candidateDirectory '_pydantic_core.cp312-win_amd64.pyd'
$loadProbePath = 'C:\P404Probe\load_probe.py'
$outputArtifactPath = Join-Path $OutputDirectory 'signed-pyd\_pydantic_core.cp312-win_amd64.pyd'
$certificate = $null
$trustStores = @()
$storeCleanupConfirmed = $false
$signResult = [ordered]@{ exit_code = $null; output = '' }
$verifyBeforeTrust = [ordered]@{ exit_code = $null; output = '' }
$verifyAfterTrust = [ordered]@{ exit_code = $null; output = '' }
$pythonResult = [ordered]@{ started = $false; exit_code = $null; stdout = ''; stderr = '' }
$failure = $null

function Get-FileSha256([string] $Path) {
    (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Invoke-SignTool([string[]] $Arguments) {
    $output = @(& $signToolPath @Arguments 2>&1 | ForEach-Object { [string]$_ })
    return [ordered]@{ exit_code = $LASTEXITCODE; output = ($output -join "`n") }
}

function Add-CurrentUserCertificate([string] $StoreName, [System.Security.Cryptography.X509Certificates.X509Certificate2] $Cert) {
    $store = New-Object System.Security.Cryptography.X509Certificates.X509Store($StoreName, 'CurrentUser')
    try {
        $store.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
        $store.Add($Cert)
        $script:trustStores += "CurrentUser\$StoreName"
    }
    finally {
        $store.Close()
    }
}

function Remove-CurrentUserCertificate([string] $StoreName, [string] $Thumbprint) {
    $store = New-Object System.Security.Cryptography.X509Certificates.X509Store($StoreName, 'CurrentUser')
    try {
        $store.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
        foreach ($item in @($store.Certificates | Where-Object { $_.Thumbprint -eq $Thumbprint })) {
            $store.Remove($item)
        }
    }
    finally {
        $store.Close()
    }
}

function Get-CodeIntegrityEvents([datetime] $SinceUtc) {
    $records = [System.Collections.Generic.List[object]]::new()
    $errors = [System.Collections.Generic.List[object]]::new()
    foreach ($eventId in @(3076, 3077, 3089, 3099)) {
        try {
            $found = Get-WinEvent -FilterHashtable @{
                LogName = 'Microsoft-Windows-CodeIntegrity/Operational'
                Id = $eventId
            } -MaxEvents 200 -ErrorAction Stop
            foreach ($record in @($found | Where-Object { $_.TimeCreated.ToUniversalTime() -ge $SinceUtc })) {
                $records.Add([ordered]@{
                    id = $record.Id
                    record_id = $record.RecordId
                    time_created_utc = $record.TimeCreated.ToUniversalTime().ToString('o')
                    activity_id = if ($record.ActivityId) { $record.ActivityId.ToString() } else { $null }
                    message = $record.Message
                    xml = $record.ToXml()
                })
            }
        }
        catch {
            if ($_.Exception.Message -notmatch 'No events were found') {
                $errors.Add([ordered]@{ id = $eventId; error = $_.Exception.Message })
            }
        }
    }
    return [ordered]@{ since_utc = $SinceUtc.ToString('o'); records = @($records); errors = @($errors) }
}

New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
New-Item -ItemType Directory -Path $candidateDirectory -Force | Out-Null
$originalHash = Get-FileSha256 $sourcePath
$sourceSignature = Get-AuthenticodeSignature -LiteralPath $sourcePath
$pythonHash = Get-FileSha256 $pythonPath
$pythonSignature = Get-AuthenticodeSignature -LiteralPath $pythonPath
$signToolHash = Get-FileSha256 $signToolPath
$signToolSignature = Get-AuthenticodeSignature -LiteralPath $signToolPath

$report = [ordered]@{
    schema_version = 1
    kind = 'phase4_p404_selfsigned_pyd_signing_probe'
    collected_at_utc = $null
    status = 'unknown'
    scope = 'Locally self-signed copy of the exact locked pydantic-core 2.50.1 Windows extension; tested only in a disposable Windows Sandbox with its existing App Control policy.'
    source = [ordered]@{
        package = 'pydantic-core'
        version = '2.50.1'
        python_tag = 'cp312-cp312-win_amd64'
        wheel_filename = 'pydantic_core-2.50.1-cp312-cp312-win_amd64.whl'
        wheel_sha256 = '132529c83901437ff642f585216831bf5fd7a91df66829907e155192ead62498'
        wheel_member = 'pydantic_core/_pydantic_core.cp312-win_amd64.pyd'
        pyd_sha256 = $originalHash
        locked_pyd_sha256 = 'd38e098d798d381cc44f867162bc6da2ebc5d77bf444f0dd00d42a1bc8484297'
        original_authenticode_status = [string]$sourceSignature.Status
    }
    signing_tool = [ordered]@{
        path = $signToolPath
        sha256 = $signToolHash
        authenticode_status = [string]$signToolSignature.Status
        signer = if ($signToolSignature.SignerCertificate) { $signToolSignature.SignerCertificate.Subject } else { $null }
    }
    runtime = [ordered]@{
        path = $pythonPath
        version = $null
        sha256 = $pythonHash
        locked_sha256 = '4d6f5f81a4bca11191c4c7c6b43632694d0a4ce74e068619d8fdc161d469859a'
        authenticode_status = [string]$pythonSignature.Status
        signer = if ($pythonSignature.SignerCertificate) { $pythonSignature.SignerCertificate.Subject } else { $null }
    }
    certificate = $null
    signing = $signResult
    signature_verification_before_local_trust = $verifyBeforeTrust
    signature_verification_after_local_trust = $verifyAfterTrust
    execution = $pythonResult
    signed_copy = [ordered]@{}
    guest = [ordered]@{
        identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        os = (Get-CimInstance -ClassName Win32_OperatingSystem).Caption
        build = (Get-CimInstance -ClassName Win32_OperatingSystem).BuildNumber
        device_guard = $null
        code_integrity_events = $null
        matching_target_events = @()
        active_enforcement_policy_present = $false
    }
    isolation = [ordered]@{
        networking_disabled_by_wsb_profile = $true
        policy_changed = $false
        certificate_trust_changes_scoped_to_disposable_guest = $true
        certificate_trust_stores = @()
        private_key_exported = $false
        plugin_behavior_executed = $false
        native_load_only = $true
        local_scratch_cleanup_confirmed = $false
        guest_teardown_required = $true
    }
    failure = $null
}

try {
    if ($originalHash -ne $report.source.locked_pyd_sha256) {
        throw "locked_pyd_hash_mismatch: $originalHash"
    }
    if ($pythonHash -ne $report.runtime.locked_sha256 -or $pythonSignature.Status -ne 'Valid') {
        throw "signed_runtime_identity_mismatch: hash=$pythonHash signature=$($pythonSignature.Status)"
    }
    if ($signToolSignature.Status -ne 'Valid') {
        throw "signtool_not_authenticode_valid: $($signToolSignature.Status)"
    }
    $runtimeProbe = @(& $pythonPath --version 2>&1 | ForEach-Object { [string]$_ })
    $report.runtime.version = ($runtimeProbe -join "`n").Trim()

    $certificate = New-SelfSignedCertificate `
        -Type CodeSigningCert `
        -Subject 'CN=Ligamina P4-04 Disposable Self-Signed Test' `
        -CertStoreLocation 'Cert:\CurrentUser\My' `
        -KeyAlgorithm RSA `
        -KeyLength 2048 `
        -HashAlgorithm SHA256 `
        -KeyExportPolicy NonExportable `
        -NotAfter (Get-Date).AddDays(1)
    $report.certificate = [ordered]@{
        subject = $certificate.Subject
        issuer = $certificate.Issuer
        thumbprint = $certificate.Thumbprint
        not_after_utc = $certificate.NotAfter.ToUniversalTime().ToString('o')
        self_issued = ($certificate.Subject -eq $certificate.Issuer)
        key_export_policy = 'NonExportable'
        code_signing_eku = @($certificate.Extensions |
            Where-Object { $_.Oid.Value -eq '2.5.29.37' } |
            ForEach-Object { $_.EnhancedKeyUsages } |
            ForEach-Object { $_.Value })
    }
    if ($certificate.Subject -ne $certificate.Issuer) { throw 'generated_certificate_is_not_self_issued' }

    Copy-Item -LiteralPath $sourcePath -Destination $candidatePath
    $signResult = Invoke-SignTool @('sign', '/fd', 'SHA256', '/s', 'My', '/sha1', $certificate.Thumbprint, $candidatePath)
    $report.signing = $signResult
    if ($signResult.exit_code -ne 0) { throw "signtool_sign_failed: $($signResult.output)" }

    $preTrust = Invoke-SignTool @('verify', '/pa', '/v', $candidatePath)
    $report.signature_verification_before_local_trust = $preTrust
    Add-CurrentUserCertificate 'Root' $certificate
    Add-CurrentUserCertificate 'TrustedPublisher' $certificate
    $postTrustSignature = Get-AuthenticodeSignature -LiteralPath $candidatePath
    $postTrust = Invoke-SignTool @('verify', '/pa', '/v', $candidatePath)
    $report.signature_verification_after_local_trust = [ordered]@{
        authenticode_status = [string]$postTrustSignature.Status
        signer = if ($postTrustSignature.SignerCertificate) { $postTrustSignature.SignerCertificate.Subject } else { $null }
        signtool = $postTrust
    }

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $pythonPath
    $startInfo.Arguments = ('"{0}" "{1}"' -f $loadProbePath, $candidatePath)
    $startInfo.WorkingDirectory = $candidateDirectory
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    if (-not $process.Start()) { throw 'python_load_probe_start_returned_false' }
    $report.execution.started = $true
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit(30000)) {
        $process.Kill()
        $process.WaitForExit()
        $report.execution.timed_out = $true
    }
    $report.execution.exit_code = if ($process.HasExited) { $process.ExitCode } else { $null }
    $report.execution.stdout = ([string]$stdoutTask.Result).TrimEnd()
    $report.execution.stderr = ([string]$stderrTask.Result).TrimEnd()
    $process.Dispose()

    $signedHash = Get-FileSha256 $candidatePath
    $report.signed_copy = [ordered]@{
        guest_path = $candidatePath
        output_path = $outputArtifactPath
        sha256 = $signedHash
        authenticode_status = [string](Get-AuthenticodeSignature -LiteralPath $candidatePath).Status
        source_hash_unchanged = ((Get-FileSha256 $sourcePath) -eq $originalHash)
    }
}
catch {
    $failure = $_.Exception.ToString()
    $report.failure = $failure
}
finally {
    if ($certificate) {
        foreach ($storeName in @('TrustedPublisher', 'Root', 'My')) {
            try { Remove-CurrentUserCertificate $storeName $certificate.Thumbprint } catch { $report.failure = (@($report.failure, $_.Exception.Message) -join "`n") }
        }
        $remaining = @()
        foreach ($storeName in @('TrustedPublisher', 'Root', 'My')) {
            $found = Get-ChildItem -Path "Cert:\CurrentUser\$storeName" -ErrorAction SilentlyContinue | Where-Object { $_.Thumbprint -eq $certificate.Thumbprint }
            if ($found) { $remaining += "CurrentUser\$storeName" }
        }
        $storeCleanupConfirmed = ($remaining.Count -eq 0)
    }
    $report.isolation.certificate_trust_stores = @($trustStores)
    $report.isolation.certificate_store_cleanup_confirmed = $storeCleanupConfirmed
    if (Test-Path -LiteralPath $candidatePath) {
        New-Item -ItemType Directory -Path (Split-Path -Parent $outputArtifactPath) -Force | Out-Null
        Copy-Item -LiteralPath $candidatePath -Destination $outputArtifactPath -Force
        $report.signed_copy = [ordered]@{
            guest_path = $candidatePath
            output_path = $outputArtifactPath
            sha256 = Get-FileSha256 $candidatePath
            output_sha256 = Get-FileSha256 $outputArtifactPath
            authenticode_status = [string](Get-AuthenticodeSignature -LiteralPath $candidatePath).Status
            source_hash_unchanged = ((Get-FileSha256 $sourcePath) -eq $originalHash)
        }
    }
    if (Test-Path -LiteralPath $candidateDirectory) {
        $expectedTempPrefix = [System.IO.Path]::GetFullPath($env:TEMP).TrimEnd('\') + '\'
        $resolvedCandidateDirectory = [System.IO.Path]::GetFullPath($candidateDirectory)
        if (-not $resolvedCandidateDirectory.StartsWith($expectedTempPrefix, [System.StringComparison]::OrdinalIgnoreCase) -or
            (Split-Path -Leaf $resolvedCandidateDirectory) -ne 'LigaminaP404SelfSigned') {
            $report.failure = (@($report.failure, 'refusing_to_remove_scratch_outside_expected_guest_temp') -join "`n")
        }
        else {
            try { Remove-Item -LiteralPath $resolvedCandidateDirectory -Recurse -Force } catch {
                $report.failure = (@($report.failure, "scratch_cleanup_failed: $($_.Exception.Message)") -join "`n")
            }
        }
    }
    $report.isolation.local_scratch_cleanup_confirmed = (-not (Test-Path -LiteralPath $candidateDirectory))
    try {
        $guard = Get-CimInstance -Namespace 'root\Microsoft\Windows\DeviceGuard' -ClassName Win32_DeviceGuard
        $report.guest.device_guard = [ordered]@{
            code_integrity_policy_enforcement_status = $guard.CodeIntegrityPolicyEnforcementStatus
            user_mode_code_integrity_policy_enforcement_status = $guard.UserModeCodeIntegrityPolicyEnforcementStatus
        }
    }
    catch { $report.guest.device_guard = [ordered]@{ error = $_.Exception.Message } }
    $events = Get-CodeIntegrityEvents -SinceUtc $probeStart.AddMinutes(-45)
    $report.guest.code_integrity_events = $events
    $report.guest.matching_target_events = @($events.records | Where-Object {
        $_.message -match [regex]::Escape((Split-Path -Leaf $candidatePath)) -or $_.xml -match [regex]::Escape((Split-Path -Leaf $candidatePath))
    })
    $report.guest.active_enforcement_policy_present = [bool](@($events.records | Where-Object {
        $_.id -eq 3099 -and $_.xml -match 'VerifiedAndReputableDesktop' -and $_.xml -match '0283ac0f-fff1-49ae-ada1-8a933130cad6'
    }).Count -gt 0)
    $blocked = [bool](@($report.guest.matching_target_events | Where-Object { $_.id -eq 3077 }).Count -gt 0)
    $loaded = $false
    if ($report.execution.stdout) {
        try { $loaded = ((ConvertFrom-Json -InputObject $report.execution.stdout -ErrorAction Stop).loaded -eq $true) } catch {}
    }
    if ($blocked -and $report.guest.active_enforcement_policy_present) { $report.status = 'blocked_by_active_enforcement_after_local_self_signing' }
    elseif ($loaded -and $report.guest.active_enforcement_policy_present) { $report.status = 'loaded_under_active_enforcement_after_local_self_signing' }
    elseif ($report.guest.active_enforcement_policy_present -and $report.execution.stdout -match '\[WinError 4551\]') { $report.status = 'blocked_by_active_enforcement_win32_4551_event_attribution_incomplete' }
    elseif ($failure) { $report.status = 'probe_failed_or_incomplete' }
    else { $report.status = 'unknown_policy_or_load_result' }
    $report.collected_at_utc = (Get-Date).ToUniversalTime().ToString('o')
    $report.isolation.networking_disabled_by_wsb_profile = $true
    $report.isolation.policy_changed = $false
    $report.isolation.private_key_exported = $false
    $report.isolation.plugin_behavior_executed = $false
    $report.isolation.native_load_only = $true
    $json = $report | ConvertTo-Json -Depth 100
    $reportPath = Join-Path $OutputDirectory 'selfsigned-pyd-probe.json'
    [System.IO.File]::WriteAllText($reportPath, $json + [Environment]::NewLine, $utf8NoBom)
    $reportHash = Get-FileSha256 $reportPath
    [System.IO.File]::WriteAllText("$reportPath.sha256", "$reportHash  $(Split-Path -Leaf $reportPath)`n", $utf8NoBom)
    Write-Output "selfsigned_pyd_probe_status=$($report.status)"
    Write-Output "sign_exit=$($report.signing.exit_code); auth=$($report.signature_verification_after_local_trust.authenticode_status); python_exit=$($report.execution.exit_code)"
    Write-Output "report=$reportPath"
}
