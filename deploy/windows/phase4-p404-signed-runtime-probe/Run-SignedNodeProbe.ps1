param(
    [Parameter(Mandatory = $true)]
    [string] $OutputDirectory
)

$ErrorActionPreference = 'Stop'
$probeStart = (Get-Date).ToUniversalTime()
$nodePath = 'C:\P404Node\node.exe'
$probePath = 'C:\P404Probe\Probe-NodeRuntime.js'
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Invoke-CapturedProcess([string] $FilePath, [string] $Arguments, [int] $TimeoutMs = 30000) {
    $process = New-Object System.Diagnostics.Process
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $FilePath
    $startInfo.Arguments = $Arguments
    $startInfo.WorkingDirectory = 'C:\P404Probe'
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process.StartInfo = $startInfo
    $startedAt = (Get-Date).ToUniversalTime()
    try {
        if (-not $process.Start()) { throw 'process_start_returned_false' }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $exited = $process.WaitForExit($TimeoutMs)
        if (-not $exited) {
            $process.Kill()
            $process.WaitForExit()
        }
        return [ordered]@{
            executable = $FilePath
            arguments = $Arguments
            pid = $process.Id
            started_at_utc = $startedAt.ToString('o')
            ended_at_utc = (Get-Date).ToUniversalTime().ToString('o')
            timed_out = (-not $exited)
            exit_code = if ($exited) { $process.ExitCode } else { $null }
            stdout = ([string]$stdoutTask.Result).TrimEnd()
            stderr = ([string]$stderrTask.Result).TrimEnd()
        }
    }
    catch {
        return [ordered]@{
            executable = $FilePath
            arguments = $Arguments
            pid = if ($process -and $process.Id) { $process.Id } else { $null }
            started_at_utc = $startedAt.ToString('o')
            ended_at_utc = (Get-Date).ToUniversalTime().ToString('o')
            timed_out = $false
            exit_code = $null
            stdout = ''
            stderr = $_.Exception.ToString()
        }
    }
    finally {
        if ($process) { $process.Dispose() }
    }
}

function Invoke-CiTool {
    $tool = Get-Command 'CiTool.exe' -ErrorAction SilentlyContinue
    if ($null -eq $tool) {
        return [ordered]@{ available = $false; path = $null; exit_code = $null; stdout = ''; stderr = 'command_not_found' }
    }
    $result = Invoke-CapturedProcess -FilePath $tool.Source -Arguments '-lp -json'
    return [ordered]@{
        available = $true
        path = $tool.Source
        exit_code = $result.exit_code
        stdout = $result.stdout
        stderr = $result.stderr
    }
}

function Get-RecentCodeIntegrityEvents([datetime] $SinceUtc) {
    $records = [System.Collections.Generic.List[object]]::new()
    $errors = [System.Collections.Generic.List[object]]::new()
    foreach ($eventId in @(3076, 3077, 3089, 3099)) {
        try {
            $found = Get-WinEvent -FilterHashtable @{
                LogName = 'Microsoft-Windows-CodeIntegrity/Operational'
                Id = $eventId
                StartTime = $SinceUtc.ToLocalTime()
            } -MaxEvents 100 -ErrorAction Stop
            foreach ($record in $found) {
                $records.Add([ordered]@{
                    time_created_utc = $record.TimeCreated.ToUniversalTime().ToString('o')
                    id = $record.Id
                    record_id = $record.RecordId
                    activity_id = if ($record.ActivityId) { $record.ActivityId.ToString() } else { $null }
                    message = $record.Message
                    xml = $record.ToXml()
                })
            }
        }
        catch {
            $errors.Add([ordered]@{ id = $eventId; error = $_.Exception.Message })
        }
    }
    return [ordered]@{ since_utc = $SinceUtc.ToString('o'); records = @($records); errors = @($errors) }
}

New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$nodeSignature = Get-AuthenticodeSignature -LiteralPath $nodePath
$nodeHash = (Get-FileHash -LiteralPath $nodePath -Algorithm SHA256).Hash.ToLowerInvariant()
$version = Invoke-CapturedProcess -FilePath $nodePath -Arguments '--version'
$env:P404_OUTPUT_DIRECTORY = $OutputDirectory
$probe = Invoke-CapturedProcess -FilePath $nodePath -Arguments ('"' + $probePath + '"')
$probeEnd = (Get-Date).ToUniversalTime()
$deviceGuard = $null
$deviceGuardError = $null
try {
    $guard = Get-CimInstance -Namespace 'root\Microsoft\Windows\DeviceGuard' -ClassName Win32_DeviceGuard
    $deviceGuard = [ordered]@{
        code_integrity_policy_enforcement_status = $guard.CodeIntegrityPolicyEnforcementStatus
        usermode_code_integrity_policy_enforcement_status = $guard.UsermodeCodeIntegrityPolicyEnforcementStatus
    }
}
catch { $deviceGuardError = $_.Exception.Message }
$ciTool = Invoke-CiTool
$events = Get-RecentCodeIntegrityEvents -SinceUtc $probeStart.AddSeconds(-2)
$probePayload = $null
if (-not [string]::IsNullOrWhiteSpace($probe.stdout)) {
    try { $probePayload = ConvertFrom-Json -InputObject $probe.stdout -ErrorAction Stop }
    catch { $probePayload = $null }
}
$runtimePassed = ($nodeSignature.Status -eq 'Valid' -and $version.exit_code -eq 0 -and -not $version.timed_out -and $probe.exit_code -eq 0 -and -not $probe.timed_out -and $probePayload.writable_mapping.marker_removed -eq $true)
$report = [ordered]@{
    schema_version = 1
    kind = 'phase4_p404_signed_runtime_probe'
    collected_at_utc = (Get-Date).ToUniversalTime().ToString('o')
    status = if ($runtimePassed) { 'passed_runtime_only' } else { 'blocked_or_failed_runtime_probe' }
    scope = 'Signed OpenJS Node executable and built-in-only JavaScript probe; no plugin installation, catalog code, policy mutation, or network request.'
    candidate = [ordered]@{
        registry_entry_id = 'ko-163c3bd4cf87c72c24210251'
        package = '@koishijs/plugin-echo'
        version = '2.2.5'
        artifact_sha256 = '79da39692ba8fce04ea597ff85696a0fe193cd89a95a91a0d459ea40ab592265'
        windows_profile_id = 'koishi-core-4.18.11-node-24.20.0-x86_64-portable-lock'
        dependency_lock_sha256 = '33e0897b794134c39215e7824a47edec3eb4acc79fcb9f4df00a03c1455f616b'
    }
    runtime = [ordered]@{
        path = $nodePath
        sha256 = $nodeHash
        signature_status = [string]$nodeSignature.Status
        signer = if ($nodeSignature.SignerCertificate) { $nodeSignature.SignerCertificate.Subject } else { $null }
        signer_thumbprint = if ($nodeSignature.SignerCertificate) { $nodeSignature.SignerCertificate.Thumbprint } else { $null }
        version_probe = $version
        built_in_probe = $probe
        parsed_probe = $probePayload
    }
    guest = [ordered]@{
        computer_name = $env:COMPUTERNAME
        os = (Get-CimInstance -ClassName Win32_OperatingSystem).Caption
        build = (Get-CimInstance -ClassName Win32_OperatingSystem).BuildNumber
        architecture = (Get-CimInstance -ClassName Win32_OperatingSystem).OSArchitecture
        identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        probe_start_utc = $probeStart.ToString('o')
        probe_end_utc = $probeEnd.ToString('o')
        device_guard = $deviceGuard
        device_guard_error = $deviceGuardError
        citool_policy_inventory = $ciTool
        code_integrity_events = $events
    }
    actions = [ordered]@{
        network_disabled_by_wsb_profile = $true
        catalog_execution = 'not_started'
        policy_changed = $false
        output_marker_cleanup_confirmed = ($probePayload.writable_mapping.marker_removed -eq $true)
        admission = 'not_evaluated'
        catalog_plugin_claims_allowed = $false
    }
    limitations = @(
        'This run proves only that this exact signed Node executable starts and executes built-in-only JavaScript in this guest profile.',
        'CiTool inventory completeness and exact base-policy mapping remain unverified if CiTool returns an error.',
        'This does not evaluate JavaScript plugin policy, plugin dependencies, Koishi host behavior, or Windows catalog admission.'
    )
}
$json = $report | ConvertTo-Json -Depth 100
$path = Join-Path $OutputDirectory 'signed-runtime-probe.json'
[System.IO.File]::WriteAllText($path, $json + [Environment]::NewLine, $utf8NoBom)
$hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
[System.IO.File]::WriteAllText("$path.sha256", "$hash  $(Split-Path -Leaf $path)`n", $utf8NoBom)
Write-Output "signed_runtime_probe_status=$($report.status)"
Write-Output "node_signature=$($nodeSignature.Status); node_exit=$($version.exit_code); probe_exit=$($probe.exit_code)"
Write-Output "report=$path"
