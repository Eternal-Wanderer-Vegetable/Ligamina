# P4-04 native probe and App Control review package

> Historical evidence: on 2026-10-28 the user narrowed P4-04 to runner behavior only. This package preserves the original unsigned-PE Enforcement observation; administrator policy review is no longer a prerequisite for the active task. The current runner-only scope and evidence are tracked in [`phase4-p404-runner-behavior-20261028`](../phase4-p404-runner-behavior-20261028/README.md). Do not interpret this historical probe as runner behavior or plugin admission.

This package records the plan-scoped, self-owned native-only probe in the disposable Windows Sandbox guest. It is an administrator review package; it contains no policy, certificate, key, or deployment script, and it does not authorize changing any policy.

## Observed guest result

- Guest: Windows 11 Enterprise, build 26100, `WDAGUtilityAccount`; kernel and user-mode Code Integrity status were both `2` in `Win32_DeviceGuard`.
- Probe: unsigned x64 native PE, built from [`native-probe.c`](native-probe.c), SHA-256 `2b55b31fdb72609e7533a98ca7b5cb492305f5f5629b247282437f55d02772c5`.
- The process was rejected before its entry point. No marker was created; no child process or network request was made.
- Code Integrity Event 3077 and two Event 3089 records share Activity ID `4d54d80c-6488-0000-b52a-554d8864dd01`. Event 3077's `SHA256 Flat Hash` equals the probe file SHA-256. It identifies policy `VerifiedAndReputableDesktop`, ID `27555.1000.240208`, GUID `{0283ac0f-fff1-49ae-ada1-8a933130cad6}`, policy hash `2668895A5B233A80432D00D67251D7B7F52686A3FB13780F4B242C5A1F937A01`, and status `0xc0e90002`.
- The correlated 3099 activation record has Options `0xb18c2714`; bit 16 (`Enabled:Audit Mode`) is clear, matching the 3077 enforced-block event. Other Evaluation/Audit policies were also present.
- `CiTool -lp -json` returned HRESULT `0x80073BC3` (requested system device not found), so full active policy inventory and `IsEnforced` data remain unavailable. Catalog admission remains `not_evaluated`; catalog plugin claims remain false.

The raw probe receipt and event XML, plus the machine-readable hash-bound review, are in `../phase4-p404-guest-admission-20261028-v2/` and [`../phase4-p404-native-probe-review-20261028.json`](../phase4-p404-native-probe-review-20261028.json). The review binds Event 3077 to two 3089 signature records by Activity ID and to the exact flat file hash.

## Historical reproduction in a fresh disposable guest (prior scope)

The binary and scripts should be mounted read-only. The existing `.wsb` package can be extended with a read-only mapping for this folder and its run-scoped writable output directory. In a running guest with an active user session, the equivalent command is:

```powershell
wsb share --id <sandbox-id> --host-path <this-folder> --sandbox-path C:\P404NativeProbe
wsb exec --id <sandbox-id> --command 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\P404NativeProbe\Run-NativeProbe.ps1' --run-as ExistingLogin
```

`Run-NativeProbe.ps1` captures the guest file hash and signature status, launch exception or exit code, marker state, and Code Integrity events. `Correlate-PolicyEvents.ps1` retains event XML for correlation. The report is written to `C:\P404Results`, a run-scoped writable share.

## Historical administrator review boundary (superseded)

Under the original P4-04 scope, an administrator review would have been required before continuing this native-probe path. The user later narrowed P4-04 to runner behavior only; this policy review is no longer a current task prerequisite. Do not add a broad path, publisher, or host exception. Do not copy a policy to or change the host.

This package does not include a `.cip`, signing certificate/private key, or install/remove commands. No policy was changed in this run. A later policy package must be independently signed and reviewed, disclose its exact hashes and target guest, and include deployment and rollback steps before use. The rollback for an approved Sandbox-only experiment is to stop that guest and recreate a fresh guest from the original `.wsb`; no host policy should be touched.

The planned native-probe rerun and catalog admission steps are superseded by the runner-only scope. The 3077 record remains historical evidence that the original PE was blocked. Current P4-04 acceptance is tracked in [`phase4-p404-runner-behavior-20261028`](../phase4-p404-runner-behavior-20261028/README.md).

## Source references

- [App Control event ID explanations](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/operations/event-id-explanations)
- [App Control event tags and policy Options](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/operations/event-tag-explanations)
- [App Control debugging and troubleshooting](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/operations/appcontrol-debugging-and-troubleshooting)
- [Windows Sandbox CLI](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-cli)
- [Win32_DeviceGuard enforcement status](https://learn.microsoft.com/en-us/windows/security/hardware-security/enable-virtualization-based-protection-of-code-integrity)
