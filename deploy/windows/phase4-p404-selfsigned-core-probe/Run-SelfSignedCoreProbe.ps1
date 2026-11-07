param(
    [Parameter(Mandatory = $true)][string] $RunId,
    [string] $ExpectedCoreSha256 = '6c6d0a390916df63ab27080fd85a9e880785854cec63c43f1ac7a0b120dbdd28'
)

$ErrorActionPreference = 'Stop'
$repo = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..'))
$corePath = Join-Path $repo 'target\release\ligamina-cli.exe'
$stage = Join-Path $repo ".work\phase4-p404-selfsigned-core-$RunId"
$output = Join-Path $repo "outputs\acceptance\phase4-p404-selfsigned-core-$RunId"
$signToolPath = 'C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\signtool.exe'
$expectedSignToolSha256 = 'e7b517a6a2828af2d1fba3da60ae1e322a95141bfae192622725329630caa2b3'

if (-not (Test-Path -LiteralPath $corePath)) { throw "Missing local Core candidate: $corePath" }
if (Test-Path -LiteralPath $stage) { throw "Refusing to reuse stage directory: $stage" }
if (Test-Path -LiteralPath $output) { throw "Refusing to overwrite evidence directory: $output" }
$coreHash = (Get-FileHash -LiteralPath $corePath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($coreHash -ne $ExpectedCoreSha256.ToLowerInvariant()) { throw "Core SHA-256 mismatch: $coreHash" }
if ((Get-AuthenticodeSignature -LiteralPath $corePath).Status -ne 'NotSigned') { throw 'Expected the local Core candidate to be unsigned' }
if (-not (Test-Path -LiteralPath $signToolPath)) { throw "Missing Windows SDK SignTool: $signToolPath" }
$signToolHash = (Get-FileHash -LiteralPath $signToolPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($signToolHash -ne $expectedSignToolSha256) { throw "SignTool SHA-256 mismatch: $signToolHash" }
if ((Get-AuthenticodeSignature -LiteralPath $signToolPath).Status -ne 'Valid') { throw 'SignTool Authenticode signature is not valid' }

New-Item -ItemType Directory -Path (Join-Path $stage 'input'), $output | Out-Null
Copy-Item -LiteralPath $corePath -Destination (Join-Path $stage 'input\ligamina-cli.exe')
Copy-Item -LiteralPath $signToolPath -Destination (Join-Path $stage 'input\signtool.exe')
$hostInputs = [ordered]@{
    kind = 'phase4_p404_selfsigned_core_host_inputs'
    collected_at_utc = (Get-Date).ToUniversalTime().ToString('o')
    run_id = $RunId
    repository_head = (& git -C $repo rev-parse HEAD).Trim()
    core_source = 'target/release/ligamina-cli.exe'
    core_sha256 = $coreHash
    core_authenticode_status = 'NotSigned'
    signtool_sha256 = $signToolHash
    signtool_authenticode_status = 'Valid'
    network_disabled = $true
    mapped_inputs_read_only = $true
}
$hostInputsPath = Join-Path $output 'host-inputs.json'
[System.IO.File]::WriteAllText($hostInputsPath, ($hostInputs | ConvertTo-Json -Depth 8) + [Environment]::NewLine, (New-Object System.Text.UTF8Encoding($false)))
Copy-Item -LiteralPath $hostInputsPath -Destination (Join-Path $stage 'input\host-inputs.json')

$wsbPath = Join-Path $stage 'phase4-p404-selfsigned-core-probe.wsb'
$wsb = @"
<Configuration>
  <VGpu>Disable</VGpu><Networking>Disable</Networking><ClipboardRedirection>Disable</ClipboardRedirection>
  <PrinterRedirection>Disable</PrinterRedirection><AudioInput>Disable</AudioInput><VideoInput>Disable</VideoInput>
  <ProtectedClient>Enable</ProtectedClient>
  <MappedFolders>
    <MappedFolder><HostFolder>$(Join-Path $stage 'input')</HostFolder><SandboxFolder>C:\P404Input</SandboxFolder><ReadOnly>true</ReadOnly></MappedFolder>
    <MappedFolder><HostFolder>$PSScriptRoot</HostFolder><SandboxFolder>C:\P404Probe</SandboxFolder><ReadOnly>true</ReadOnly></MappedFolder>
    <MappedFolder><HostFolder>$output</HostFolder><SandboxFolder>C:\P404Output</SandboxFolder><ReadOnly>false</ReadOnly></MappedFolder>
  </MappedFolders>
  <LogonCommand><Command>powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\P404Probe\Run-SelfSignedCoreProbe-Guest.ps1 -OutputDirectory C:\P404Output</Command></LogonCommand>
</Configuration>
"@
[System.IO.File]::WriteAllText($wsbPath, $wsb, (New-Object System.Text.UTF8Encoding($false)))

$existing = @((wsb list --raw | ConvertFrom-Json).WindowsSandboxEnvironments)
if ($existing.Count -ne 0) { throw 'Refusing to start while another Windows Sandbox guest is active' }
$beforeIds = @($existing | ForEach-Object { $_.Id })
$reportPath = Join-Path $output 'selfsigned-core-probe.json'
$wsbArgument = '"' + $wsbPath + '"'
Start-Process -FilePath (Join-Path $env:WINDIR 'System32\WindowsSandbox.exe') -ArgumentList $wsbArgument -WindowStyle Hidden
$deadline = [DateTime]::UtcNow.AddMinutes(10)
while (-not (Test-Path -LiteralPath $reportPath) -and [DateTime]::UtcNow -lt $deadline) { Start-Sleep -Seconds 5 }
if (-not (Test-Path -LiteralPath $reportPath)) {
    $current = @((wsb list --raw | ConvertFrom-Json).WindowsSandboxEnvironments)
    $owned = @($current | Where-Object { $beforeIds -notcontains $_.Id })
    foreach ($sandbox in $owned) { wsb stop --id $sandbox.Id }
    $remaining = @((wsb list --raw | ConvertFrom-Json).WindowsSandboxEnvironments)
    $progressPath = Join-Path $output 'guest-progress.jsonl'
    $lastProgress = if (Test-Path -LiteralPath $progressPath) { (Get-Content -LiteralPath $progressPath | Select-Object -Last 1) } else { 'no_guest_progress_written' }
    if ($remaining.Count -ne 0) { throw "Timed out and Sandbox teardown is not confirmed; progress=$lastProgress; inspect wsb list --raw: $output" }
    throw "Timed out waiting for the guest report; owned Sandbox stopped and teardown confirmed; last progress=$lastProgress; preserve stage/output: $output"
}

$current = @((wsb list --raw | ConvertFrom-Json).WindowsSandboxEnvironments)
$owned = @($current | Where-Object { $beforeIds -notcontains $_.Id })
foreach ($sandbox in $owned) { wsb stop --id $sandbox.Id }
if (@((wsb list --raw | ConvertFrom-Json).WindowsSandboxEnvironments).Count -ne 0) { throw 'Sandbox teardown is not confirmed' }

$guestReport = Get-Content -LiteralPath $reportPath -Raw | ConvertFrom-Json
$signedCopyPath = Join-Path $output 'signed-core\ligamina-cli.exe'
$hostSignedCopyHash = (Get-FileHash -LiteralPath $signedCopyPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($hostSignedCopyHash -ne $guestReport.source_core.signed_copy_sha256) { throw 'Signed Core hash does not match guest report' }

function Write-Sha256Sidecar([string] $Path) {
    $hash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    [System.IO.File]::WriteAllText("$Path.sha256", "$hash  $([System.IO.Path]::GetFileName($Path))" + [Environment]::NewLine, [System.Text.Encoding]::ASCII)
    return $hash
}
$inputHash = Write-Sha256Sidecar $hostInputsPath
$guestHash = Write-Sha256Sidecar $reportPath
$signedArtifactHash = Write-Sha256Sidecar $signedCopyPath
$progressPath = Join-Path $output 'guest-progress.jsonl'
$progressHash = if (Test-Path -LiteralPath $progressPath) { Write-Sha256Sidecar $progressPath } else { $null }
$review = [ordered]@{
    schema_version = 1
    kind = 'phase4_p404_selfsigned_core_review'
    reviewed_at_utc = (Get-Date).ToUniversalTime().ToString('o')
    status = $guestReport.status
    scope = 'One-shot execution of --version for the hash-bound local Ligamina release Core binary, signed only as a derivative inside a disposable Windows Sandbox.'
    source_core_sha256 = $coreHash
    signed_core_sha256 = $hostSignedCopyHash
    source_core_unchanged = $true
    signature_verification = $guestReport.signature_verification
    certificate = $guestReport.certificate
    execution = $guestReport.execution
    enforcement = $guestReport.enforcement
    isolation = [ordered]@{
        host_certificate_stores_modified = $false
        host_app_control_policy_modified = $false
        guest_certificate_cleanup_confirmed = $guestReport.isolation.certificate_cleanup_confirmed
        sandbox_teardown_confirmed = $true
        active_sandboxes_after_run = 0
        catalog_plugin_or_behavior_executed = $false
    }
    interpretation = 'This runtime-only probe does not establish Core/Host federated behavior, catalog admission, or release-signing readiness.'
    source_reports = [ordered]@{
        'host-inputs.json' = $inputHash
        'selfsigned-core-probe.json' = $guestHash
        'signed-core/ligamina-cli.exe' = $signedArtifactHash
        'guest-progress.jsonl' = $progressHash
    }
    report_hashes_excluded_from_this_document = $true
}
$reviewPath = Join-Path $output 'selfsigned-core-review.json'
[System.IO.File]::WriteAllText($reviewPath, ($review | ConvertTo-Json -Depth 20) + [Environment]::NewLine, (New-Object System.Text.UTF8Encoding($false)))
$reviewHash = Write-Sha256Sidecar $reviewPath
Write-Output "Core source SHA-256: $coreHash"
Write-Output "Signed copy SHA-256: $hostSignedCopyHash"
Write-Output "Probe status: $($guestReport.status)"
Write-Output "Review SHA-256: $reviewHash"
