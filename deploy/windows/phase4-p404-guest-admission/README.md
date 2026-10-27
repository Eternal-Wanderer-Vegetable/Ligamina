# P4-04 disposable Sandbox guest admission collection

This package collects read-only evidence from the Windows Sandbox guest before any Windows catalog pilot. It records the guest OS/runtime identity, `Win32_DeviceGuard` fields, active App Control policy inventory from `CiTool -lp -json`, and recent Code Integrity Operational events 3076, 3077, 3089, and 3099.

The `.wsb` disables networking, vGPU, clipboard, printer, microphone, and camera redirection. It maps this package read-only and maps only the empty, run-scoped result directory as writable. Its startup command uses a batch launcher that records its exit status and runs the collector as the Sandbox account. The collector does not update or remove policies, load native binaries, or execute catalog plugins. It records `admission=not_evaluated`; the results must be reviewed before any admission decision.

Run from an elevated PowerShell only if Windows Sandbox itself requests elevation:

```powershell
New-Item -ItemType Directory -Force outputs/acceptance/phase4-p404-guest-admission-20261028 | Out-Null
Start-Process "$env:WINDIR\System32\WindowsSandbox.exe" -ArgumentList (Resolve-Path deploy/windows/phase4-p404-guest-admission/phase4-p404-guest-admission.wsb).Path
```

Wait for `logon-command.log`, `guest-admission.json`, `codeintegrity-events.json`, and both `.sha256` sidecars to appear in the mapped result directory. Review the launcher exit status, exact policy IDs, base IDs, enforced/audit modes, collection errors, and event activity IDs. Do not treat the host preflight or a guest with only Evaluation/Audit policies as Enforcement admission.
