# Clean Windows Sandbox acceptance

`Ligamina-C14.wsb` maps this checkout into a disposable Windows Sandbox and runs the T15 package acceptance script. The bootstrap records which runtime commands are present before installation, places the installer scratch directory under a Chinese path containing spaces, and writes the C14 reports back to `outputs/acceptance/`.

Before opening the Sandbox, run `prepare-c14-runtime-cache.ps1` on the host. It downloads the pinned runtime archives and plugin wheel, verifies their SHA-256 hashes, and warms the locked uv/npm dependency caches. It also performs a fresh offline install of the locked NoneBot autohelp profile. The Sandbox rechecks the staged hashes and installs from those offline caches. Direct Sandbox egress can time out when the host uses a loopback-only proxy; the environment report records that limitation.

The host needs the Windows Sandbox optional feature. From an elevated PowerShell session, enable it with:

```powershell
Enable-WindowsOptionalFeature -Online -FeatureName Containers-DisposableClientVM -All
```

Alternatively, run `enable-windows-sandbox-admin.ps1` from an elevated PowerShell session. It records the feature result and restarts Windows after the feature is enabled. Then open `Ligamina-C14.wsb`. Keep the mapped Ligamina folder available until the Sandbox has written both acceptance reports.
