# P4-04 disposable self-signed Core runtime probe

This diagnostic asks whether a one-shot --version launch of the local Ligamina release Core works after a disposable Windows Sandbox creates a local code-signing certificate and signs a copied executable. The source is mounted read-only; the guest network and peripheral redirection are disabled. The private key is non-exportable and remains in the disposable guest. Run v8 temporarily adds the public certificate only to guest `CurrentUser\Root` and `CurrentUser\TrustedPublisher`; it does not alter App Control policy or host trust.

The probe does not start a Host, install or execute a catalog plugin, or run Federated behavior. Even a successful launch is runtime-only evidence. The signed copy is a derivative of a local build, not a release artifact or accepted production signing identity.

## Locked inputs

- Core: target/release/ligamina-cli.exe, SHA-256 6c6d0a390916df63ab27080fd85a9e880785854cec63c43f1ac7a0b120dbdd28, Authenticode NotSigned.
- SignTool: Windows SDK SignTool SHA-256 e7b517a6a2828af2d1fba3da60ae1e322a95141bfae192622725329630caa2b3, Authenticode Valid.
- GitHub release preflight: [machine-readable snapshot](../../../outputs/acceptance/phase4-p404-core-release-signing-preflight-20261028.json) and matching .sha256; the query returned zero releases.

The latest run is `20261029-v8-guest-trust`. Run from the repository root in PowerShell:

~~~powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File deploy/windows/phase4-p404-selfsigned-core-probe/Run-SelfSignedCoreProbe.ps1 -RunId 20261029-v8-guest-trust
~~~

The host refuses to overwrite a run directory or start while any Windows Sandbox is active. The guest writes guest-progress.jsonl before and after each major phase. SignTool, Core, and CiTool subprocesses have timeouts and redirected output. If the guest report does not appear within ten minutes, the host stops only the Sandbox instance created by this run and confirms teardown; it preserves all output for review.

## Preserved attempts

- The first attempt used Import-Certificate, which raised the non-interactive UI error before signing. Its report is outputs/acceptance/phase4-p404-selfsigned-core-20261028/selfsigned-core-probe.json.
- Run 20261028-v2 used direct X509Store operations. The mapped signed copy was written with SHA-256 016906e14b31bc434a54bfe97d5640bc606c01bb74af4711e8910a788f50325b, but no guest report appeared within the ten-minute window. The exact Sandbox was stopped and wsb list --raw returned zero active guests. This is an incomplete probe; it gives no runtime or enforcement result.
- Run 20261028-v3 wrote progress through input verification and creation of a non-exportable certificate, then stopped advancing before the trust-store phase completed. The guest subsequently disappeared from wsb list --raw and the active guest count returned to zero; an explicit stop found no remaining registered instance. No host trust or policy change occurred. Its partial progress log is preserved in outputs/acceptance/phase4-p404-selfsigned-core-20261028-v3/guest-progress.jsonl; the unavailable guest report means no guest certificate cleanup or runtime claim can be made.
- Run 20261028-v4 avoided guest Root/TrustedPublisher insertion and recorded SignTool's successful signing text, but Start-Process returned a null exit code. Core execution was withheld; its report is incomplete.
- Run 20261028-v5 used .NET Process for redirected output and exit status. SignTool signing exited 0; verification exited 1 because the self-issued root was untrusted (no guest trust-store change); Core launch was blocked with Win32 4551. The report initially used unknown because the classifier incorrectly required a new 3099 event.
- Run 20261028-v6 corrected that classification and reproduced the same result: SignTool sign exit 0, verify exit 1 for an untrusted root, and Core process start blocked by the current Application Control policy with Win32 4551. It did not query events 3033/3034 or preserve the baseline 3099 records.
- Run 20261028-v7 added 3033/3034 queries and captured 20 baseline Event 3099 policy records. Record 1039 identifies VerifiedAndReputableDesktop / 27555.1000.240208 / GUID {0283ac0f-fff1-49ae-ada1-8a933130cad6}, Options 0xb18c2714, hash 2668895A5B233A80432D00D67251D7B7F52686A3FB13780F4B242C5A1F937A01. SignTool sign exited 0; verification exited 1 because the self-issued root was untrusted; Core process start was blocked by the current Application Control policy with Win32 4551. No post-start 3033/3034/3076/3077/3089/3099 event or Activity ID was captured, so exact file/event attribution remains incomplete. DeviceGuard kernel/user status were both 2; CiTool returned HRESULT 0x80073BC3. Guest My-store certificate cleanup and Sandbox teardown were confirmed; Root/TrustedPublisher, host trust, and policy were unchanged. Review: outputs/acceptance/phase4-p404-selfsigned-core-20261028-v7/selfsigned-core-review.json, SHA-256 651b7101ad006704b1bf6d138bbf9fd1ddce7ac0498f9918c0e211233c936830. Signed derivative SHA-256 029cbcb31a51a81bbf6dfa48f653ff6fedcc2ca943af6ec816bcd50d6f7ee541.

The self-signed .pyd experiment is separately documented at [self-signed signature probe](../phase4-p404-selfsigned-signature-probe/README.md). That test added trust only inside its disposable guest, verified the signed derivative, and still observed Win32 4551 during the native load attempt. Neither experiment grants catalog admission or changes the current P4-04 blocker.

## Latest run: 20261029-v8-guest-trust

The guest added its generated public certificate to CurrentUser Root and TrustedPublisher. SignTool signing and `/pa` verification both exited 0; signed derivative SHA-256 is `593914a2e2b59589f53b39164419a1d1fe5595ef293a887f78643ee27a702b09`. Core `--version` was still blocked before process start with Win32 4551. The run captured 20 baseline Event 3099 records; record 1039 identifies `VerifiedAndReputableDesktop` / ID `27555.1000.240208` / GUID `{0283ac0f-fff1-49ae-ada1-8a933130cad6}` / Options `0xb18c2714` / hash `2668895A5B233A80432D00D67251D7B7F52686A3FB13780F4B242C5A1F937A01`. No post-start 3033/3034/3076/3077/3089/3099 event or Activity ID was captured, so exact file/event attribution remains incomplete. DeviceGuard fields were 2/2; CiTool returned HRESULT `0x80073BC3`.

Certificate cleanup is recorded as incomplete: removal from guest CurrentUser My and TrustedPublisher was confirmed, while removal from CurrentUser Root returned “The operation was canceled by the user.” The machine report marks `certificate_cleanup_confirmed=false`. The disposable Sandbox was torn down, `wsb list --raw` returned zero guests, and all six host CurrentUser/LocalMachine My/Root/TrustedPublisher stores were checked and contained no matching thumbprint. Host policy and source Core were unchanged. No catalog plugin, Host, or behavior ran. Review: [v8 machine review](../../../outputs/acceptance/phase4-p404-selfsigned-core-20261029-v8-guest-trust/selfsigned-core-review.json), SHA-256 `276b55c334540f3450554284ed702d58521c86b63429bd715d60225b45e8ed87`; the structured guest report and matching sidecars are preserved in the same output directory.

Local follow-up verification is recorded in [phase4-p404-selfsigned-core-followup-verification-20261029.json](../../../outputs/acceptance/phase4-p404-selfsigned-core-followup-verification-20261029.json), SHA-256 `4233b763b8ab23d59236e3d86f1b5eb095d5dd070c11e6022e814e2104c662fa`. It checks docs/readme gates, evidence-hash checks, PowerShell AST, v8 sidecars, host stores, Sandbox teardown, and whitespace; it does not assert behavior admission.
