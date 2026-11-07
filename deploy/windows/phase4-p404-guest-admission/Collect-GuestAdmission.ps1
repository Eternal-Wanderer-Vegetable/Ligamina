param(
    [Parameter(Mandatory = $true)]
    [string] $OutputDirectory
)

$ErrorActionPreference = 'Stop'
$eventIds = @(3076, 3077, 3089, 3099)
$startTime = (Get-Date).AddDays(-14)
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function ConvertTo-PlainObject($Value) {
    if ($null -eq $Value) { return $null }
    if ($Value -is [System.Array]) {
        return @($Value | ForEach-Object { ConvertTo-PlainObject $_ })
    }
    if ($Value -is [System.Management.Automation.PSCustomObject]) {
        $result = [ordered]@{}
        foreach ($property in $Value.PSObject.Properties) {
            $result[$property.Name] = ConvertTo-PlainObject $property.Value
        }
        return $result
    }
    return $Value
}

function Get-CommandCapture([string] $FilePath, [string[]] $Arguments) {
    $command = Get-Command $FilePath -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        return [ordered]@{ available = $false; command_path = $null; exit_code = $null; stdout = ''; stderr = 'command_not_found' }
    }

    $process = $null
    try {
        $startInfo = New-Object System.Diagnostics.ProcessStartInfo
        $startInfo.FileName = $command.Source
        $startInfo.Arguments = [string]::Join(' ', $Arguments)
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true

        $process = New-Object System.Diagnostics.Process
        $process.StartInfo = $startInfo
        if (-not $process.Start()) {
            throw 'process_start_returned_false'
        }

        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        $stdout = $stdoutTask.Result
        $stderr = $stderrTask.Result
        return [ordered]@{
            available = $true
            command_path = $command.Source
            exit_code = $process.ExitCode
            stdout = ([string]$stdout).TrimEnd()
            stderr = ([string]$stderr).TrimEnd()
        }
    }
    catch {
        $exitCode = $null
        if ($process) {
            try {
                if ($process.HasExited) { $exitCode = $process.ExitCode }
            }
            catch {
                $exitCode = $null
            }
        }
        return [ordered]@{
            available = $true
            command_path = $command.Source
            exit_code = $exitCode
            stdout = ''
            stderr = $_.Exception.ToString()
        }
    }
}

New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$os = Get-CimInstance -ClassName Win32_OperatingSystem
$computer = Get-CimInstance -ClassName Win32_ComputerSystem
$deviceGuard = $null
$deviceGuardError = $null
try {
    $deviceGuard = Get-CimInstance -Namespace 'root\Microsoft\Windows\DeviceGuard' -ClassName Win32_DeviceGuard
}
catch {
    $deviceGuardError = $_.Exception.Message
}

$ciTool = Get-CommandCapture -FilePath 'CiTool.exe' -Arguments @('-lp', '-json')
$ciToolPolicies = $null
$ciToolJsonError = $null
if ($ciTool.exit_code -eq 0 -and -not [string]::IsNullOrWhiteSpace($ciTool.stdout)) {
    try {
        $ciToolPolicies = ConvertFrom-Json -InputObject $ciTool.stdout -ErrorAction Stop
    }
    catch {
        $ciToolJsonError = $_.Exception.Message
    }
}
$events = [System.Collections.Generic.List[object]]::new()
$eventErrors = [System.Collections.Generic.List[object]]::new()
foreach ($eventId in $eventIds) {
    try {
        $records = Get-WinEvent -FilterHashtable @{
            LogName = 'Microsoft-Windows-CodeIntegrity/Operational'
            Id = $eventId
            StartTime = $startTime
        } -MaxEvents 100 -ErrorAction Stop

        foreach ($record in $records) {
            $activityId = $null
            if ($record.ActivityId) { $activityId = $record.ActivityId.ToString() }
            $events.Add([ordered]@{
                time_created = $record.TimeCreated.ToUniversalTime().ToString('o')
                id = $record.Id
                record_id = $record.RecordId
                activity_id = $activityId
                level = $record.LevelDisplayName
                provider = $record.ProviderName
                message = $record.Message
                xml = $record.ToXml()
            })
        }
    }
    catch {
        $eventErrors.Add([ordered]@{ id = $eventId; error = $_.Exception.Message })
    }
}

$report = [ordered]@{
    schema_version = 1
    kind = 'phase4_p404_windows_guest_admission_collection'
    collected_at_utc = (Get-Date).ToUniversalTime().ToString('o')
    collection_window_start_utc = $startTime.ToUniversalTime().ToString('o')
    guest = [ordered]@{
        computer_name = $computer.Name
        manufacturer = $computer.Manufacturer
        model = $computer.Model
        os_caption = $os.Caption
        version = $os.Version
        build = $os.BuildNumber
        architecture = $os.OSArchitecture
        powershell_version = $PSVersionTable.PSVersion.ToString()
        current_identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    }
    win32_device_guard = if ($deviceGuard) {
        [ordered]@{
            code_integrity_policy_enforcement_status = $deviceGuard.CodeIntegrityPolicyEnforcementStatus
            usermode_code_integrity_policy_enforcement_status = $deviceGuard.UsermodeCodeIntegrityPolicyEnforcementStatus
            security_services_configured = @($deviceGuard.SecurityServicesConfigured)
            security_services_running = @($deviceGuard.SecurityServicesRunning)
            code_integrity_policies_enforced = $deviceGuard.CodeIntegrityPoliciesEnforced
            code_integrity_policies_audit = $deviceGuard.CodeIntegrityPoliciesAudit
        }
    } else { $null }
    win32_device_guard_error = $deviceGuardError
    citool_list_policies = $ciTool
    citool_policy_inventory = $ciToolPolicies
    citool_json_error = $ciToolJsonError
    code_integrity_event_ids = $eventIds
    code_integrity_events_collected = $events.Count
    code_integrity_event_errors = @($eventErrors)
    catalog_execution = 'not_started'
    native_probe = 'not_started'
    admission = 'not_evaluated'
    boundary = 'Read-only guest inventory only; no policy mutation, native probe, or catalog plugin execution.'
}

$reportPath = Join-Path $OutputDirectory 'guest-admission.json'
$eventsPath = Join-Path $OutputDirectory 'codeintegrity-events.json'
$reportJson = $report | ConvertTo-Json -Depth 100
$eventsJson = @($events) | ConvertTo-Json -Depth 20
[System.IO.File]::WriteAllText($reportPath, $reportJson + [Environment]::NewLine, $utf8NoBom)
[System.IO.File]::WriteAllText($eventsPath, $eventsJson + [Environment]::NewLine, $utf8NoBom)

foreach ($path in @($reportPath, $eventsPath)) {
    $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    $sidecar = "$hash  $(Split-Path -Leaf $path)"
    [System.IO.File]::WriteAllText("$path.sha256", $sidecar + [Environment]::NewLine, $utf8NoBom)
}

Write-Output "P4-04 guest inventory written to $OutputDirectory"
Write-Output "Code Integrity events collected: $($events.Count); admission remains not_evaluated."
