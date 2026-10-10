# Windows x64 runtime bundle

`runtime-manifest.json` pins the uv, managed CPython, and Node runtime inputs. The Python profile uses uv's managed CPython 3.12.14 build because Python.org publishes 3.12.14 as a source-only security release without a Windows installer. The Node zip and uv archive are checked against fixed SHA-256 values before extraction.

## Build and install

Run from a Windows x64 development checkout with the pinned Rust toolchain and Node 24.20.0:

```powershell
pwsh -File deploy/windows/build-bundle.ps1
pwsh -File .work/releases/ligamina-win-x64/deploy/windows/install.ps1 `
  -PayloadRoot .work/releases/ligamina-win-x64 `
  -InstallRoot "$env:LOCALAPPDATA/Programs/Ligamina" `
  -Profile nonebot,koishi
```

The installer creates a versioned candidate and only updates `active.json` after the selected runtimes, locked dependencies, and Core executable pass their checks. Runtime files and Host environments remain under the application directory. The Python install disables registry registration and executable shims; neither Python nor Node is added to the machine PATH. Updates retain the previous version directory for recovery.

Start the active Core with:

```powershell
pwsh -File .work/releases/ligamina-win-x64/deploy/windows/run.ps1 `
  -InstallRoot "$env:LOCALAPPDATA/Programs/Ligamina" `
  -ConfigPath "$env:APPDATA/Ligamina/daemon.json"
```

`daemon.json` must point to the selected Host executable under the active version (`hosts/nonebot/.venv/Scripts/python.exe` or `runtimes/node-v24.20.0/node.exe`) and keep its database and credentials under the user data directory. A first-run configuration wizard and one-click clean-machine acceptance are still open T15 work; this bundle is not a G2-ready installer yet.

Uninstall removes application versions and leaves `%APPDATA%/Ligamina` untouched by default. `-RemoveUserData` is required to delete that separate directory.

## Runtime sources

- [Python 3.12.14 release](https://www.python.org/downloads/release/python-31214/)
- [uv 0.12.5 release and artifact checksums](https://github.com/astral-sh/uv/releases/tag/0.12.5)
- [Node.js 24.20.0 archive](https://nodejs.org/en/download/archive/v24.20.0)
