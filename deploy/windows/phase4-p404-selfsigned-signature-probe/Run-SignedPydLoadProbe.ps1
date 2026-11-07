param(
    [Parameter(Mandatory = $true)]
    [string] $OutputDirectory
)

$ErrorActionPreference = 'Stop'
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$probeStart = (Get-Date).ToUniversalTime()
$signedPath = Join-Path $OutputDirectory 'signed-pyd\_pydantic_core.cp312-win_amd64.pyd'
$pythonPath = 'C:\P404Input\runtime\python.exe'
$signToolPath = 'C:\P404Input\tools\signtool.exe'
$loadProbePath = 'C:\P404Probe\load_probe.py'
$candidateDirectory = Join-Path $env:TEMP 'LigaminaP404SelfSignedRetry'
$candidatePath = Join-Path $candidateDirectory '_pydantic_core.cp312-win_amd64.pyd'
$certificate = $null
$addedStores = @()
$cleanupConfirmed = $false
$execution = [ordered]@{ started = $false; exit_code = $null; stdout = ''; stderr = ''; timed_out = $false }
$events = [System.Collections.Generic.List[object]]::new()
$eventErrors = [System.Collections.Generic.List[object]]::new()
$failure = $null
$trustedSignature = $null
$trustedVerification = [ordered]@{ exit_code = $null; output = '' }

function Get-FileSha256([string] $Path) {
    (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Add-GuestCertificate([string] $StoreName, [System.Security.Cryptography.X509Certificates.X509Certificate2] $Cert) {
    $store = New-Object System.Security.Cryptography.X509Certificates.X509Store($StoreName, 'CurrentUser')
    try {
        $store.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
        $store.Add($Cert)
        $script:addedStores += "CurrentUser\$StoreName"
    }
    finally { $store.Close() }
}

function Remove-GuestCertificate([string] $StoreName, [string] $Thumbprint) {
    $store = New-Object System.Security.Cryptography.X509Certificates.X509Store($StoreName, 'CurrentUser')
    try {
        $store.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
        foreach ($item in @($store.Certificates | Where-Object { $_.Thumbprint -eq $Thumbprint })) {
            $store.Remove($item)
        }
    }
    finally { $store.Close() }
}

function Invoke-SignTool([string[]] $Arguments) {
    $lines = @(& $signToolPath @Arguments 2>&1 | ForEach-Object { [string]$_ })
    return [ordered]@{ exit_code = $LASTEXITCODE; output = ($lines -join "`n") }
}

function Collect-CodeIntegrityEvents([datetime] $SinceUtc) {
    foreach ($eventId in @(3076, 3077, 3089, 3099)) {
        try {
            $records = Get-WinEvent -FilterHashtable @{
                LogName = 'Microsoft-Windows-CodeIntegrity/Operational'
                Id = $eventId
            } -MaxEvents 200 -ErrorAction Stop
            foreach ($record in @($records | Where-Object { $_.TimeCreated.ToUniversalTime() -ge $SinceUtc })) {
                $events.Add([ordered]@{
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
                $eventErrors.Add([ordered]@{ id = $eventId; error = $_.Exception.Message })
            }
        }
    }
}

try {
    if (-not (Test-Path -LiteralPath $signedPath)) { throw 'signed_copy_missing' }
    $signedHash = Get-FileSha256 $signedPath
    $signature = Get-AuthenticodeSignature -LiteralPath $signedPath
    $certificate = $signature.SignerCertificate
    if ($null -eq $certificate) { throw "embedded_signer_certificate_missing: $($signature.Status)" }
    if ($certificate.Subject -ne $certificate.Issuer) { throw 'embedded_signer_is_not_self_issued' }

    Add-GuestCertificate 'Root' $certificate
    Add-GuestCertificate 'TrustedPublisher' $certificate
    $trustedSignature = Get-AuthenticodeSignature -LiteralPath $signedPath
    $trustedVerification = Invoke-SignTool @('verify', '/pa', '/v', $signedPath)

    New-Item -ItemType Directory -Path $candidateDirectory -Force | Out-Null
    Copy-Item -LiteralPath $signedPath -Destination $candidatePath
    if ((Get-FileSha256 $candidatePath) -ne $signedHash) { throw 'local_candidate_hash_mismatch' }

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
    $execution.started = $true
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit(30000)) {
        $process.Kill()
        $process.WaitForExit()
        $execution.timed_out = $true
    }
    $execution.exit_code = if ($process.HasExited) { $process.ExitCode } else { $null }
    $execution.stdout = ([string]$stdoutTask.Result).TrimEnd()
    $execution.stderr = ([string]$stderrTask.Result).TrimEnd()
    $process.Dispose()
}
catch { $failure = $_.Exception.ToString() }
finally {
    Collect-CodeIntegrityEvents -SinceUtc $probeStart.AddMinutes(-5)
    $matchingEvents = @($events | Where-Object {
        $_.message -match [regex]::Escape((Split-Path -Leaf $candidatePath)) -or
        $_.xml -match [regex]::Escape((Split-Path -Leaf $candidatePath))
    })
    $policyEvents = @($events | Where-Object {
        $_.id -eq 3099 -and $_.xml -match 'VerifiedAndReputableDesktop' -and
        $_.xml -match '0283ac0f-fff1-49ae-ada1-8a933130cad6'
    })

    if ($certificate) {
        foreach ($storeName in @('TrustedPublisher', 'Root', 'My')) {
            try { Remove-GuestCertificate $storeName $certificate.Thumbprint } catch {
                $failure = (@($failure, $_.Exception.Message) -join "`n")
            }
        }
        $remaining = @()
        foreach ($storeName in @('TrustedPublisher', 'Root', 'My')) {
            $found = Get-ChildItem -Path "Cert:\CurrentUser\$storeName" -ErrorAction SilentlyContinue |
                Where-Object { $_.Thumbprint -eq $certificate.Thumbprint }
            if ($found) { $remaining += "CurrentUser\$storeName" }
        }
        $cleanupConfirmed = ($remaining.Count -eq 0)
    }
    if (Test-Path -LiteralPath $candidateDirectory) {
        $tempPrefix = [System.IO.Path]::GetFullPath($env:TEMP).TrimEnd('\') + '\'
        $resolvedCandidateDirectory = [System.IO.Path]::GetFullPath($candidateDirectory)
        if ($resolvedCandidateDirectory.StartsWith($tempPrefix, [System.StringComparison]::OrdinalIgnoreCase) -and
            (Split-Path -Leaf $resolvedCandidateDirectory) -eq 'LigaminaP404SelfSignedRetry') {
            try { Remove-Item -LiteralPath $resolvedCandidateDirectory -Recurse -Force } catch {
                $failure = (@($failure, "guest_scratch_cleanup_failed: $($_.Exception.Message)") -join "`n")
            }
        }
    }

    $guard = $null
    try {
        $deviceGuard = Get-CimInstance -Namespace 'root\Microsoft\Windows\DeviceGuard' -ClassName Win32_DeviceGuard
        $guard = [ordered]@{
            code_integrity_policy_enforcement_status = $deviceGuard.CodeIntegrityPolicyEnforcementStatus
            user_mode_code_integrity_policy_enforcement_status = $deviceGuard.UserModeCodeIntegrityPolicyEnforcementStatus
        }
    }
    catch { $guard = [ordered]@{ error = $_.Exception.Message } }

    $blocked = [bool](@($matchingEvents | Where-Object { $_.id -eq 3077 }).Count -gt 0)
    $loaded = $false
    if ($execution.stdout) {
        try { $loaded = ((ConvertFrom-Json -InputObject $execution.stdout -ErrorAction Stop).loaded -eq $true) } catch {}
    }
    if ($blocked -and $policyEvents.Count -gt 0) { $status = 'blocked_by_active_enforcement_after_local_self_signing' }
    elseif ($loaded -and $policyEvents.Count -gt 0) { $status = 'loaded_under_active_enforcement_after_local_self_signing' }
    elseif ($policyEvents.Count -gt 0 -and $execution.stdout -match '\[WinError 4551\]') { $status = 'blocked_by_active_enforcement_win32_4551_event_attribution_incomplete' }
    elseif ($failure) { $status = 'probe_failed_or_incomplete' }
    else { $status = 'unknown_policy_or_load_result' }

    $report = [ordered]@{
        schema_version = 1
        kind = 'phase4_p404_selfsigned_pyd_signing_probe'
        collected_at_utc = (Get-Date).ToUniversalTime().ToString('o')
        status = $status
        scope = 'Native LoadLibrary of the locally self-signed pydantic-core 2.50.1 Windows extension in the exact P4-04 Windows Sandbox profile; no catalog plugin behavior.'
        source = [ordered]@{
            package = 'pydantic-core'
            version = '2.50.1'
            python_tag = 'cp312-cp312-win_amd64'
            wheel_filename = 'pydantic_core-2.50.1-cp312-cp312-win_amd64.whl'
            wheel_sha256 = '132529c83901437ff642f585216831bf5fd7a91df66829907e155192ead62498'
            wheel_member = 'pydantic_core/_pydantic_core.cp312-win_amd64.pyd'
            original_pyd_sha256 = 'd38e098d798d381cc44f867162bc6da2ebc5d77bf444f0dd00d42a1bc8484297'
            signed_copy_sha256 = if (Test-Path -LiteralPath $signedPath) { Get-FileSha256 $signedPath } else { $null }
            host_input_report = 'host-inputs.json'
        }
        runtime = [ordered]@{
            path = $pythonPath
            version = '3.12.10'
            sha256 = Get-FileSha256 $pythonPath
            locked_sha256 = '4d6f5f81a4bca11191c4c7c6b43632694d0a4ce74e068619d8fdc161d469859a'
            authenticode_status = [string](Get-AuthenticodeSignature -LiteralPath $pythonPath).Status
            signer = (Get-AuthenticodeSignature -LiteralPath $pythonPath).SignerCertificate.Subject
        }
        certificate = if ($certificate) {
            [ordered]@{
                subject = $certificate.Subject
                issuer = $certificate.Issuer
                thumbprint = $certificate.Thumbprint
                not_after_utc = $certificate.NotAfter.ToUniversalTime().ToString('o')
                self_issued = ($certificate.Subject -eq $certificate.Issuer)
                public_certificate_only_imported = (-not $certificate.HasPrivateKey)
                private_key_exported = $false
            }
        } else { $null }
        signature = [ordered]@{
            embedded_signer = if ($certificate) { $certificate.Subject } else { $null }
            status_after_guest_local_trust = if ($trustedSignature) { [string]$trustedSignature.Status } else { $null }
            signtool_verification_after_guest_local_trust = $trustedVerification
            trust_scope = @($addedStores)
            trust_store_cleanup_confirmed = $cleanupConfirmed
        }
        execution = $execution
        guest = [ordered]@{
            identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
            os = (Get-CimInstance -ClassName Win32_OperatingSystem).Caption
            build = (Get-CimInstance -ClassName Win32_OperatingSystem).BuildNumber
            device_guard = $guard
            enforcement_policy_activation_events = @($policyEvents)
            matching_target_events = $matchingEvents
            all_code_integrity_events = [ordered]@{
                since_utc = $probeStart.AddMinutes(-5).ToString('o')
                records = @($events)
                errors = @($eventErrors)
            }
        }
        isolation = [ordered]@{
            networking_disabled_by_wsb_profile = $true
            app_control_policy_changed = $false
            guest_current_user_trust_stores_changed_temporarily = $true
            private_key_exported = $false
            plugin_behavior_executed = $false
            native_load_only = $true
            guest_scratch_cleanup_confirmed = (-not (Test-Path -LiteralPath $candidateDirectory))
            guest_teardown_required = $true
        }
        limitations = @(
            'This run tests one hash-locked native dependency using an experimental self-signed derivative; it does not modify or replace the frozen wheel.',
            'Local guest trust does not establish that the self-signed issuer belongs to the Microsoft Trusted Root Program or that the original dependency profile is admitted.',
            'Even an allowed native load is not plugin behavior compatibility or release evidence.'
        )
        failure = $failure
    }
    $reportPath = Join-Path $OutputDirectory 'selfsigned-pyd-probe.json'
    [System.IO.File]::WriteAllText($reportPath, ($report | ConvertTo-Json -Depth 100) + [Environment]::NewLine, $utf8NoBom)
    $reportHash = Get-FileSha256 $reportPath
    [System.IO.File]::WriteAllText("$reportPath.sha256", "$reportHash  $(Split-Path -Leaf $reportPath)`n", $utf8NoBom)
    Write-Output "selfsigned_pyd_probe_status=$status"
    Write-Output "python_exit=$($execution.exit_code); matching_ci_events=$($matchingEvents.Count); enforcement_events=$($policyEvents.Count)"
    Write-Output "report=$reportPath; sha256=$reportHash"
}
