# P4-04 signed Node runtime probe

This package performs a narrowly scoped, offline Windows Sandbox probe of the exact Node runtime selected by the locked Koishi Windows profile. It runs the signed `node.exe` and a small JavaScript file that uses only Node built-ins, verifies the writable evidence mount, and removes its temporary marker. It does not install or load a catalog plugin, make network requests, or change App Control policy.

The runtime is mounted read-only from `E:\stella\Ligamina-artifact-cache\p305-koishi-lock-runtime`. The candidate Koishi archive and all 172 package-lock archives were checked against their frozen SHA-256/SHA-512 identities before this probe. The Node executable hash/signature and guest policy-event observations are included in the resulting JSON. A successful runtime probe is not catalog admission or plugin compatibility.

The included `.wsb` records the configuration used for the completed run and maps output to `outputs/acceptance/phase4-p404-signed-runtime-20261028/`. That folder now contains evidence; do not launch this configuration again as-is. For a repeat, create a new empty run directory, change the output `<HostFolder>` in the `.wsb` to that path, then launch:

```powershell
$runId = Get-Date -Format 'yyyyMMddTHHmmss'
$outputPath = "outputs/acceptance/phase4-p404-signed-runtime-$runId"
New-Item -ItemType Directory -Path $outputPath | Out-Null
# Update the .wsb output HostFolder to this new directory before launching.
Start-Process "$env:WINDIR\System32\WindowsSandbox.exe" -ArgumentList (Resolve-Path deploy/windows/phase4-p404-signed-runtime-probe/phase4-p404-signed-runtime-probe.wsb).Path -WindowStyle Hidden
```

Wait for `signed-runtime-probe.json` and its `.sha256` sidecar. Review the exact Node signature/hash, process exit and version, marker cleanup, Device Guard fields, CiTool result, and Code Integrity events. Stop the guest with the Windows Sandbox CLI and verify that the active guest list is empty. Do not reuse an output folder.
