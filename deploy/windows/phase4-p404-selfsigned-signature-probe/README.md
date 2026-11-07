# P4-04 disposable self-signed code-signing probe

This probe asks whether a locally issued Authenticode code-signing certificate can satisfy the active Windows App Control Enforcement policy for the exact `pydantic-core==2.50.1` CPython 3.12.10 extension that was blocked in the P4-04 Windows Sandbox load attempt.

It extracts the `.pyd` only from the already hash-locked wheel, copies it into a fresh disposable Windows Sandbox, creates a non-exportable self-signed code-signing certificate in that guest, signs the copy with Microsoft SignTool, adds the public certificate to that guest user's `Root` and `TrustedPublisher` stores, and attempts a native `LoadLibrary` through the separately locked PSF-signed CPython runtime. The original wheel, host certificate stores, App Control policy, and locked inputs remain unchanged. Networking is disabled. The private key never leaves the disposable guest. The signed output is a separate experiment artifact and must not be treated as the frozen wheel or profile artifact.

The experiment tests signing and native-load policy only. It does not install or execute a catalog plugin, invoke a Host, or provide Windows behavior admission. A successful `signtool verify` or `Get-AuthenticodeSignature` result proves cryptographic signature verification under the guest's explicitly scoped trust setup; only the Code Integrity event and active policy state can establish whether this specific enforcement profile admitted the module.

## Inputs and fixed identities

- Profile lock: `outputs/acceptance/phase4-p404-nonebot-python31210-profile-lock-20261028.json`, exact profile `nonebot-2.5.0-onebot-2.4.6-py3.12.10-windows-x86_64`.
- Wheel: `pydantic_core-2.50.1-cp312-cp312-win_amd64.whl`, SHA-256 `132529c83901437ff642f585216831bf5fd7a91df66829907e155192ead62498`.
- Wheel member: `pydantic_core/_pydantic_core.cp312-win_amd64.pyd`, SHA-256 `d38e098d798d381cc44f867162bc6da2ebc5d77bf444f0dd00d42a1bc8484297`.
- Runtime: Python.org embeddable CPython 3.12.10, SHA-256 `4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3`; executable SHA-256 `4d6f5f81a4bca11191c4c7c6b43632694d0a4ce74e068619d8fdc161d469859a`, Authenticode signer Python Software Foundation.
- Microsoft SignTool: `C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\signtool.exe`; the run records its hash and Authenticode identity.

## Reproduce

Use a new, empty output directory each run. The preparation commands verify both locked cache inputs before extraction and do not modify the source archive:

```powershell
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path .).Path
$run = 'phase4-p404-selfsigned-code-signing-20261028'
$stage = Join-Path $repo ".work\$run"
$out = Join-Path $repo "outputs\acceptance\$run"
$deploy = Join-Path $repo 'deploy\windows\phase4-p404-selfsigned-signature-probe'
if (Test-Path $stage) { throw "Refusing to reuse existing stage: $stage" }
if (Test-Path $out) { throw "Refusing to overwrite existing evidence: $out" }
New-Item -ItemType Directory -Path (Join-Path $stage 'input\runtime'),(Join-Path $stage 'input\pyd'),(Join-Path $stage 'input\tools'),$out | Out-Null

$cache = 'E:\stella\Ligamina-artifact-cache'
$runtimeArchive = Join-Path $cache 'windows-runtime-cache\sha256\4a\4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3'
$wheel = Join-Path $cache 'wheel-cache\sha256\13\132529c83901437ff642f585216831bf5fd7a91df66829907e155192ead62498'
if ((Get-FileHash $runtimeArchive -Algorithm SHA256).Hash.ToLowerInvariant() -ne '4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3') { throw 'Runtime archive hash mismatch' }
if ((Get-FileHash $wheel -Algorithm SHA256).Hash.ToLowerInvariant() -ne '132529c83901437ff642f585216831bf5fd7a91df66829907e155192ead62498') { throw 'Wheel hash mismatch' }
$runtimeZip = Join-Path $stage 'input\python-3.12.10-embed-amd64.zip'
Copy-Item -LiteralPath $runtimeArchive -Destination $runtimeZip
Expand-Archive -LiteralPath $runtimeZip -DestinationPath (Join-Path $stage 'input\runtime')
$env:P404_STAGE = $stage
Copy-Item -LiteralPath $wheel -Destination (Join-Path $stage 'input\pyd\pydantic-core.whl')
@'
import os
import zipfile
from pathlib import Path
root=Path(os.environ['P404_STAGE'])
wheel=root/'input/pyd/pydantic-core.whl'
member='pydantic_core/_pydantic_core.cp312-win_amd64.pyd'
out=root/'input/pyd/_pydantic_core.cp312-win_amd64.pyd'
with zipfile.ZipFile(wheel) as archive:
    out.write_bytes(archive.read(member))
'@ | python -
if ((Get-FileHash (Join-Path $stage 'input\pyd\_pydantic_core.cp312-win_amd64.pyd') -Algorithm SHA256).Hash.ToLowerInvariant() -ne 'd38e098d798d381cc44f867162bc6da2ebc5d77bf444f0dd00d42a1bc8484297') { throw 'Locked wheel member hash mismatch' }
$signTool = 'C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\signtool.exe'
if ((Get-AuthenticodeSignature -LiteralPath $signTool).Status -ne 'Valid') { throw 'SignTool is not Authenticode-valid' }
Copy-Item -LiteralPath $signTool -Destination (Join-Path $stage 'input\tools\signtool.exe')

$wsb = Join-Path $stage 'phase4-p404-selfsigned-signature-probe.wsb'
$wsbXml = @"
<Configuration>
  <VGpu>Disable</VGpu><Networking>Disable</Networking><ClipboardRedirection>Disable</ClipboardRedirection>
  <PrinterRedirection>Disable</PrinterRedirection><AudioInput>Disable</AudioInput><VideoInput>Disable</VideoInput>
  <ProtectedClient>Enable</ProtectedClient>
  <MappedFolders>
    <MappedFolder><HostFolder>$stage\input</HostFolder><SandboxFolder>C:\P404Input</SandboxFolder><ReadOnly>true</ReadOnly></MappedFolder>
    <MappedFolder><HostFolder>$deploy</HostFolder><SandboxFolder>C:\P404Probe</SandboxFolder><ReadOnly>true</ReadOnly></MappedFolder>
    <MappedFolder><HostFolder>$out</HostFolder><SandboxFolder>C:\P404Output</SandboxFolder><ReadOnly>false</ReadOnly></MappedFolder>
  </MappedFolders>
  <LogonCommand><Command>powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\P404Probe\Run-SelfSignedPydProbe.ps1 -OutputDirectory C:\P404Output</Command></LogonCommand>
</Configuration>
"@
[System.IO.File]::WriteAllText($wsb, $wsbXml, (New-Object System.Text.UTF8Encoding($false)))
if ((wsb list --raw | ConvertFrom-Json).WindowsSandboxEnvironments.Count -ne 0) { throw 'Refusing to start while another Windows Sandbox guest is active' }
Start-Process -FilePath "$env:WINDIR\System32\WindowsSandbox.exe" -ArgumentList @("`"$wsb`"") -WindowStyle Hidden
```

Wait for the guest report without reusing the run directory. Then stop the guest and verify teardown:

```powershell
$report = Join-Path $out 'selfsigned-pyd-probe.json'
$deadline = [DateTime]::UtcNow.AddMinutes(10)
while (-not (Test-Path $report) -and [DateTime]::UtcNow -lt $deadline) { Start-Sleep -Seconds 5 }
if (-not (Test-Path $report)) { throw 'Timed out waiting for the guest report; inspect wsb list --raw and preserve this run directory' }
$sandboxes = (wsb list --raw | ConvertFrom-Json).WindowsSandboxEnvironments
foreach ($sandbox in $sandboxes) { wsb stop --id $sandbox.Id }
if ((wsb list --raw | ConvertFrom-Json).WindowsSandboxEnvironments.Count -ne 0) { throw 'Sandbox teardown is not confirmed' }
$document = Get-Content -LiteralPath $report -Raw | ConvertFrom-Json
$actual = (Get-FileHash -LiteralPath $report -Algorithm SHA256).Hash.ToLowerInvariant()
if ((Get-Content -LiteralPath "$report.sha256" -Raw) -notmatch [regex]::Escape($actual)) { throw 'Report sidecar mismatch' }
$document | Select-Object status,source,certificate,signing,signature_verification_after_local_trust,execution,guest,isolation
```

## Current run

The 2026-10-28 run is recorded in [the machine review](../../../outputs/acceptance/phase4-p404-selfsigned-code-signing-20261028/selfsigned-signature-review.json), SHA-256 `7b3be69a418a8adb3ec289f6a028371faff51e379df3f8e82c6ee2cba0da26ae`. The guest-local certificate (`3521DF91D8C1FCC95B5A02E299039D6A87C79831`) signed the isolated `.pyd` copy; `Get-AuthenticodeSignature` returned `Valid` and SignTool verification exited 0 after guest-only Root/TrustedPublisher trust. The locked CPython 3.12.10 load attempt still returned Win32 4551. Event 3099 recorded the active `VerifiedAndReputableDesktop` policy, but the run captured no matching 3077/3089, so file/hash/activity attribution is incomplete. The signed derivative SHA-256 is `bbb78eb03d6d720709829bbe5e5c18b96cd68d73967aa3fde5b74e03d410f7e2`; the original wheel and member remained unchanged, guest trust/scratch cleanup and Sandbox teardown were confirmed, and no catalog plugin ran. Raw guest diagnostics are `guest-postrun.json` and `guest-postrun2.json` in the run output directory. Follow-up local verification is recorded in [selfsigned-followup-verification.json](../../../outputs/acceptance/phase4-p404-selfsigned-code-signing-20261028/selfsigned-followup-verification.json), SHA-256 `7179c1a766bf2d28c3f49f043157d6cf76422920888f6b8769b10616a01d1755`; it validates docs, report sidecars, and probe syntax only, not behavior admission. Preserve the signed copy as an experimental derivative; do not replace the wheel member, package cache, profile lock, or catalog artifact with it.
