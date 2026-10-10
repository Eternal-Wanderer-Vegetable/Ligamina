[CmdletBinding()]
param(
    [string]$PayloadRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "Programs\Ligamina"),
    [string[]]$Profile = @("nonebot", "koishi"),
    [string]$ManifestPath = (Join-Path $PSScriptRoot "runtime-manifest.json")
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Get-FullPath([string]$Path) {
    return [System.IO.Path]::GetFullPath($Path)
}

function Assert-ContainedPath([string]$Parent, [string]$Child) {
    $parentFull = (Get-FullPath $Parent).TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
    $childFull = Get-FullPath $Child
    $prefix = $parentFull + [System.IO.Path]::DirectorySeparatorChar
    if (-not $childFull.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to operate outside '$parentFull': '$childFull'"
    }
}

function Invoke-Checked([string]$Executable, [string[]]$Arguments, [string]$Description) {
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE"
    }
}

function Get-VerifiedArchive($Runtime, [string]$CacheRoot) {
    $archivePath = Join-Path $CacheRoot $Runtime.archive
    Invoke-WebRequest -UseBasicParsing -Uri $Runtime.url -OutFile $archivePath -TimeoutSec 300
    $actualHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $Runtime.sha256) {
        throw "SHA256 mismatch for $($Runtime.archive): expected $($Runtime.sha256), got $actualHash"
    }
    return $archivePath
}

function Write-AtomicJson([string]$Path, $Value) {
    $parent = Split-Path -Parent $Path
    $temporary = Join-Path $parent (".active-" + [guid]::NewGuid().ToString("N") + ".tmp")
    $json = $Value | ConvertTo-Json -Depth 16
    [System.IO.File]::WriteAllText($temporary, $json + [Environment]::NewLine, (New-Object System.Text.UTF8Encoding($false)))
    if (Test-Path -LiteralPath $Path) {
        $backup = Join-Path $parent (".active-" + [guid]::NewGuid().ToString("N") + ".bak")
        try {
            [System.IO.File]::Replace($temporary, $Path, $backup)
        }
        finally {
            Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
        }
    }
    else {
        [System.IO.File]::Move($temporary, $Path)
    }
}

$profileNames = @($Profile | ForEach-Object { $_.Split(',') } | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ } | Sort-Object -Unique)
if ($profileNames.Count -eq 0 -or @($profileNames | Where-Object { $_ -notin @("nonebot", "koishi") }).Count -gt 0) {
    throw "Profile must contain nonebot, koishi, or both."
}

$payloadFull = Get-FullPath $PayloadRoot
$installFull = Get-FullPath $InstallRoot
$manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
if ($manifest.schema_version -ne 1 -or $manifest.platform -ne "windows-x64") {
    throw "Unsupported runtime manifest schema or platform."
}
if (-not (Test-Path -LiteralPath (Join-Path $payloadFull "target\release\ligamina-cli.exe"))) {
    throw "Missing target/release/ligamina-cli.exe. Build or unpack the Windows release bundle first."
}
$contractsSource = Join-Path $payloadFull "contracts"
foreach ($schemaName in @("event-envelope.schema.json", "platform-operation.schema.json", "capability-descriptor.schema.json")) {
    if (-not (Test-Path -LiteralPath (Join-Path $contractsSource "v0.1\$schemaName"))) {
        throw "Missing contracts/v0.1/$schemaName in the release bundle."
    }
}

$versionsRoot = Join-Path $installFull "versions"
$activePath = Join-Path $installFull "active.json"
New-Item -ItemType Directory -Force -Path $versionsRoot | Out-Null
$versionId = "$($manifest.application_version)-$(Get-Date -Format 'yyyyMMddHHmmss')-$([guid]::NewGuid().ToString('N').Substring(0,8))"
$candidate = Join-Path $versionsRoot $versionId
Assert-ContainedPath $versionsRoot $candidate
New-Item -ItemType Directory -Force -Path $candidate | Out-Null
$activated = $false

try {
    Copy-Item -LiteralPath $contractsSource -Destination $candidate -Recurse -Force
    $cacheRoot = Join-Path $candidate "downloads"
    $runtimeRoot = Join-Path $candidate "runtimes"
    New-Item -ItemType Directory -Force -Path $cacheRoot, $runtimeRoot | Out-Null
    $licensesRoot = Join-Path $candidate "licenses"
    New-Item -ItemType Directory -Force -Path $licensesRoot | Out-Null
    $evidence = [ordered]@{
        schema_version = 1
        application_version = $manifest.application_version
        version_id = $versionId
        platform = "windows-x64"
        profiles = $profileNames
        runtimes = [ordered]@{}
        created_at = [DateTime]::UtcNow.ToString("o")
    }

    if ($profileNames -contains "nonebot") {
        $uvRuntime = $manifest.runtimes.uv
        $uvArchive = Get-VerifiedArchive $uvRuntime $cacheRoot
        $uvRoot = Join-Path $runtimeRoot "uv-$($uvRuntime.version)"
        New-Item -ItemType Directory -Force -Path $uvRoot | Out-Null
        Expand-Archive -LiteralPath $uvArchive -DestinationPath $uvRoot -Force
        $uvExe = Join-Path $uvRoot "uv.exe"
        if (-not (Test-Path -LiteralPath $uvExe)) { throw "uv.exe was absent from the verified archive." }
        $uvVersion = (& $uvExe --version | Out-String).Trim()
        if ($uvVersion -notmatch [regex]::Escape($uvRuntime.version)) { throw "Unexpected uv version: $uvVersion" }

        $pythonRuntime = $manifest.runtimes.python
        $pythonRoot = Join-Path $runtimeRoot "python-$($pythonRuntime.version)"
        Invoke-Checked $uvExe @("python", "install", $pythonRuntime.version, "--install-dir", $pythonRoot, "--no-registry", "--no-bin", "--no-progress") "Install managed CPython"
        $pythonExe = Get-ChildItem -LiteralPath $pythonRoot -Recurse -File -Filter "python.exe" |
            Where-Object { $_.FullName -notmatch "\\Lib\\" } |
            Select-Object -First 1 -ExpandProperty FullName
        if (-not $pythonExe) { throw "uv did not install python.exe under the application runtime directory." }
        $pythonVersion = (& $pythonExe --version | Out-String).Trim()
        if ($pythonVersion -ne "Python $($pythonRuntime.version)") { throw "Unexpected Python version: $pythonVersion" }

        $nonebotSource = Join-Path $payloadFull "hosts\nonebot"
        $nonebotTarget = Join-Path $candidate "hosts\nonebot"
        New-Item -ItemType Directory -Force -Path $nonebotTarget | Out-Null
        Copy-Item -LiteralPath (Join-Path $nonebotSource "pyproject.toml"), (Join-Path $nonebotSource "uv.lock") -Destination $nonebotTarget -Force
        Copy-Item -LiteralPath (Join-Path $nonebotSource "ligamina_nonebot") -Destination $nonebotTarget -Recurse -Force
        $previousUvProjectEnvironment = $env:UV_PROJECT_ENVIRONMENT
        $env:UV_PROJECT_ENVIRONMENT = Join-Path $nonebotTarget ".venv"
        try {
            Invoke-Checked $uvExe @("sync", "--project", $nonebotTarget, "--locked", "--no-dev", "--python", $pythonExe) "Install locked NoneBot profile"
        }
        finally {
            if ($null -eq $previousUvProjectEnvironment) {
                Remove-Item Env:\UV_PROJECT_ENVIRONMENT -ErrorAction SilentlyContinue
            }
            else {
                $env:UV_PROJECT_ENVIRONMENT = $previousUvProjectEnvironment
            }
        }
        $nonebotPython = Join-Path $nonebotTarget ".venv\Scripts\python.exe"
        Invoke-Checked $nonebotPython @("-c", "import nonebot, nonebot.adapters.onebot.v11; print(nonebot.__version__)") "Verify NoneBot runtime"

        foreach ($licenseName in $uvRuntime.license_files) {
            $sourceLicense = Join-Path (Split-Path -Parent $ManifestPath) $licenseName
            if (-not (Test-Path -LiteralPath $sourceLicense)) { throw "Missing required license file: $sourceLicense" }
            Copy-Item -LiteralPath $sourceLicense -Destination $licensesRoot -Force
        }
        $pythonLicense = Join-Path (Split-Path -Parent $pythonExe) $pythonRuntime.license_file
        if (-not (Test-Path -LiteralPath $pythonLicense)) { throw "Missing managed Python license file: $pythonLicense" }
        Copy-Item -LiteralPath $pythonLicense -Destination $licensesRoot -Force
        $evidence.runtimes.uv = [ordered]@{ version = $uvRuntime.version; sha256 = $uvRuntime.sha256; license = $uvRuntime.license }
        $evidence.runtimes.python = [ordered]@{ version = $pythonRuntime.version; distribution = $pythonRuntime.distribution; executable = $pythonExe; executable_sha256 = (Get-FileHash -LiteralPath $pythonExe -Algorithm SHA256).Hash.ToLowerInvariant(); license = $pythonRuntime.license; license_sha256 = (Get-FileHash -LiteralPath $pythonLicense -Algorithm SHA256).Hash.ToLowerInvariant() }
    }

    if ($profileNames -contains "koishi") {
        $nodeRuntime = $manifest.runtimes.node
        $nodeArchive = Get-VerifiedArchive $nodeRuntime $cacheRoot
        $nodeExtract = Join-Path $runtimeRoot "node-extract"
        New-Item -ItemType Directory -Force -Path $nodeExtract | Out-Null
        Expand-Archive -LiteralPath $nodeArchive -DestinationPath $nodeExtract -Force
        $nodeSource = Join-Path $nodeExtract "node-v$($nodeRuntime.version)-win-x64"
        $nodeRoot = Join-Path $runtimeRoot "node-v$($nodeRuntime.version)"
        if (-not (Test-Path -LiteralPath (Join-Path $nodeSource "node.exe"))) { throw "node.exe was absent from the verified archive." }
        Move-Item -LiteralPath $nodeSource -Destination $nodeRoot
        Assert-ContainedPath $runtimeRoot $nodeExtract
        Remove-Item -LiteralPath $nodeExtract -Recurse -Force
        $nodeExe = Join-Path $nodeRoot "node.exe"
        $nodeVersion = (& $nodeExe --version | Out-String).Trim()
        if ($nodeVersion -ne "v$($nodeRuntime.version)") { throw "Unexpected Node version: $nodeVersion" }

        $koishiSource = Join-Path $payloadFull "hosts\koishi"
        $koishiTarget = Join-Path $candidate "hosts\koishi"
        if (-not (Test-Path -LiteralPath (Join-Path $koishiSource "dist\worker.js"))) { throw "Missing built hosts/koishi/dist/worker.js." }
        New-Item -ItemType Directory -Force -Path $koishiTarget | Out-Null
        Copy-Item -LiteralPath (Join-Path $koishiSource "package.json"), (Join-Path $koishiSource "package-lock.json") -Destination $koishiTarget -Force
        Copy-Item -LiteralPath (Join-Path $koishiSource "dist") -Destination $koishiTarget -Recurse -Force
        Push-Location $koishiTarget
        try {
            Invoke-Checked $nodeExe @((Join-Path $nodeRoot "node_modules\npm\bin\npm-cli.js"), "ci", "--omit=dev") "Install locked Koishi profile"
            Invoke-Checked $nodeExe @((Join-Path $nodeRoot "node_modules\npm\bin\npm-cli.js"), "ls", "--omit=dev", "--depth=0") "Verify Koishi dependency tree"
            Invoke-Checked $nodeExe @("--input-type=module", "-e", "import('@koishijs/core').then(() => console.log('Koishi core import passed'))") "Import Koishi core"
        }
        finally {
            Pop-Location
        }

        Copy-Item -LiteralPath (Join-Path $nodeRoot $nodeRuntime.license_file) -Destination $licensesRoot -Force
        $evidence.runtimes.node = [ordered]@{ version = $nodeRuntime.version; archive = $nodeRuntime.archive; sha256 = $nodeRuntime.sha256; license = $nodeRuntime.license }
    }

    $cliTarget = Join-Path $candidate "ligamina-cli.exe"
    Copy-Item -LiteralPath (Join-Path $payloadFull "target\release\ligamina-cli.exe") -Destination $cliTarget -Force
    $cliVersionOutput = (& $cliTarget --help | Out-String)
    if ($LASTEXITCODE -ne 0 -or $cliVersionOutput -notmatch "daemon") { throw "Packaged ligamina-cli.exe failed its help check." }
    $evidence.core = [ordered]@{ path = $cliTarget; sha256 = (Get-FileHash -LiteralPath $cliTarget -Algorithm SHA256).Hash.ToLowerInvariant() }
    $evidencePath = Join-Path $candidate "runtime-evidence.json"
    $evidence | ConvertTo-Json -Depth 16 | Set-Content -LiteralPath $evidencePath -Encoding UTF8
    if (Test-Path -LiteralPath $cacheRoot) {
        Assert-ContainedPath $candidate $cacheRoot
        Remove-Item -LiteralPath $cacheRoot -Recurse -Force
    }

    $relativeVersionPath = "versions\$versionId"
    $active = [ordered]@{ schema_version = 1; application_version = $manifest.application_version; version_id = $versionId; version_path = $relativeVersionPath; profiles = $profileNames; activated_at = [DateTime]::UtcNow.ToString("o") }
    Write-AtomicJson $activePath $active
    $activated = $true
    Write-Output ("Ligamina installed and activated: " + $candidate)
    Write-Output ("User data remains separate at: " + (Join-Path $env:APPDATA "Ligamina"))
}
catch {
    if (-not $activated -and (Test-Path -LiteralPath $candidate)) {
        Assert-ContainedPath $versionsRoot $candidate
        Remove-Item -LiteralPath $candidate -Recurse -Force
    }
    throw
}
