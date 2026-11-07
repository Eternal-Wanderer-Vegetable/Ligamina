# P4-04 disposable Sandbox guest admission collection

This is the active read-only policy inventory package for the restored P4-04 Enforcement scope. It collects guest OS/runtime identity, `Win32_DeviceGuard` fields, `CiTool -lp -json`, and Code Integrity events before any catalog pilot. The completed runner-only result remains a separate subtask in [`phase4-p404-runner-behavior-20261028`](../../../outputs/acceptance/phase4-p404-runner-behavior-20261028/README.md).

This package collects read-only evidence from the Windows Sandbox guest before any Windows catalog pilot. It records the guest OS/runtime identity, `Win32_DeviceGuard` enforcement fields, active App Control policy inventory from `CiTool -lp -json`, and recent Code Integrity Operational events 3076, 3077, 3089, and 3099.

The `.wsb` disables networking, vGPU, clipboard, printer, microphone, and camera redirection. It maps this package read-only and maps only the run-scoped result directory as writable. The collector captures CiTool output through the .NET process API, stores both parsed policy inventory and raw stdout/stderr/exit status, and writes JSON plus SHA-256 sidecars. It does not update or remove policies, load native binaries, or execute catalog plugins. It records `admission=not_evaluated`; review the evidence before making an admission decision.

The v2 and v3 folders contain completed evidence and must not be reused. The `.wsb` currently maps the new, empty run folder `outputs/acceptance/phase4-p404-guest-admission-20261028-v4/` as writable output. The v3 run still could not read the full `CiTool` inventory; this v4 package is available for a read-only collection retry if the next session has a supported way to query it. Confirm that folder is empty before launching:

```powershell
Start-Process "$env:WINDIR\System32\WindowsSandbox.exe" -ArgumentList (Resolve-Path deploy/windows/phase4-p404-guest-admission/phase4-p404-guest-admission.wsb).Path
```

Wait for `logon-command.log`, `guest-admission.json`, `codeintegrity-events.json`, and both `.sha256` sidecars. Review the launcher exit status, `Win32_DeviceGuard` kernel/user-mode enforcement values, exact active policy IDs/base IDs/enforced flags, collection errors, and event activity IDs. Do not treat host preflight or a guest with only Evaluation/Audit policies as Enforcement admission. CiTool reports policy identity and active enforcement state; its `IsEnforced` field is distinct from an OS Code Integrity enforcement status. Do not launch a catalog plugin while the current policy identity/base mapping remains unresolved.
