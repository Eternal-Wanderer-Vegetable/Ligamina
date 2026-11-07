# P4-04 Koishi Windows behavior probe

This probe runs one frozen P4 behavior fixture through the repository's stock Koishi `Context` / `Session` / `Bot.dispatch` runner inside the existing Windows Sandbox Worker. It does not call the plugin handler directly. The Worker runs with Networking disabled, a read-only input share, a low-integrity disposable process account, bounded process/memory limits, and cleanup-on-close. npm installation is offline and lifecycle scripts are disabled.

`Run-KoishiBehavior.ps1` verifies the staged OpenJS-signed Node 24.20.0 runtime, installs the exact frozen lock from bundled archives, then starts `Execute-KoishiBehavior.mjs`. The Node launcher invokes `lab/baselines/koishi/runner.mjs` with the frozen stimulus and Host behavior fixture. The custom output collector appends only recent Code Integrity events 3076, 3077, 3089, and 3099 to the Worker output so the host can bind run-time events before the Sandbox closes.

The result is one Native Host behavior observation for one case. Select one of the frozen executable positive, negative, or isolation cases with `--case-id`; the default remains isolation so the earlier run1–run5 reports remain reproducible. Each case is a separate acceptance item and needs three fresh Sandbox runs. These results do not establish Federated parity, the full Windows catalog, NoneBot admission, or real-platform delivery. Policy inventory that CiTool cannot enumerate remains unknown. Do not change App Control policy to make this probe pass.

Reproduce from the repository root with:

```powershell
$env:UV_PROJECT_ENVIRONMENT = (Join-Path (Get-Location) '.work\phase4-p404-koishi-behavior-venv')
uv run --locked --project lab --python 3.12.14 python scripts/phase4-p404-koishi-behavior.py --case-id ko-koishijs-plugin-echo-positive --output outputs/acceptance/phase4-p404-koishi-behavior-20261028-positive-run1.json
```

For the remaining frozen cases, use `--case-id ko-koishijs-plugin-echo-negative` or `--case-id ko-koishijs-plugin-echo-isolation`. Use a new output path for every independent Sandbox run. The JSON report has a SHA-256 sidecar and binds its selected fixture, package artifact, dependency lock, runtime, probe sources (including this driver), Worker result, policy events, and Oracle findings.
