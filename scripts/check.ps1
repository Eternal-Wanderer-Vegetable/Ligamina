param()

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $false)][string[]]$Arguments = @()
    )
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Executable $($Arguments -join ' ') failed with exit code $LASTEXITCODE."
    }
}

try {
    $rustVersion = (& rustc --version | Out-String).Trim()
    $uvVersion = (& uv --version | Out-String).Trim()
    $nodeVersion = (& node --version | Out-String).Trim()
    if ($rustVersion -notmatch '^rustc 1\.97\.1(?:\s|$)') { throw "Expected rustc 1.97.1; found '$rustVersion'." }
    if ($uvVersion -notmatch '^uv 0\.12\.5(?:\s|$)') { throw "Expected uv 0.12.5; found '$uvVersion'." }
    if ($nodeVersion -ne 'v24.20.0') { throw "Expected Node v24.20.0; found '$nodeVersion'." }

    Invoke-Checked node @('scripts/check-docs.mjs')
    Invoke-Checked uv @('python', 'install', '3.12.14')
    Invoke-Checked uv @('run', '--locked', '--project', 'hosts/nonebot', '--python', '3.12.14', 'pytest', '-q', 'hosts/nonebot/tests')
    Invoke-Checked uv @('run', '--locked', '--project', 'hosts/nonebot', '--python', '3.12.14', 'python', '-m', 'unittest', 'discover', '-s', 'lab/tests', '-v')
    Invoke-Checked uv @('run', '--locked', '--project', 'hosts/nonebot', '--python', '3.12.14', 'python', 'hosts/nonebot/tests/native_smoke.py')
    Invoke-Checked uv @('run', '--locked', '--project', 'hosts/nonebot', '--python', '3.12.14', 'python', 'hosts/nonebot/tests/native_bridge.py')
    Invoke-Checked uv @('run', '--locked', '--project', 'hosts/nonebot', '--python', '3.12.14', 'python', 'hosts/nonebot/tests/worker_smoke.py')

    Push-Location hosts/koishi
    try {
        Invoke-Checked npm @('ci')
        Invoke-Checked npm @('test')
        Invoke-Checked npm @('run', 'test:mcp')
    }
    finally { Pop-Location }

    Push-Location clients/reference
    try {
        Invoke-Checked npm @('ci')
        Invoke-Checked npm @('test')
    }
    finally { Pop-Location }

    Invoke-Checked cargo @('fmt', '--all', '--', '--check')
    Invoke-Checked cargo @('test', '--workspace', '--locked')
    Invoke-Checked cargo @('build', '--release', '--locked')

    New-Item -ItemType Directory -Force outputs/acceptance | Out-Null
    Invoke-Checked uv @('run', '--locked', '--project', 'lab', '--python', '3.12.14', 'python', 'lab/run.py', '--suite', 'smoke', '--platform', 'windows', '--report', 'outputs/acceptance/ci-smoke-windows.json')
    Invoke-Checked pwsh @('-NoProfile', '-File', 'deploy/windows/build-bundle.ps1')
}
finally {
    Pop-Location
}
