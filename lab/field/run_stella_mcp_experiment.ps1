<#
P6 field experiment: frozen Koishi / NoneBot / AstrBot plugins called through Stella's own
MCP client. Each run uses a fresh Core, fresh ledger and fresh network-disabled Host
containers, then checks shutdown, container cleanup and the Gateway operation count.

    powershell -NoProfile -File lab/field/run_stella_mcp_experiment.ps1 -Run run1

Nothing is sent to any chat platform. The running Stella bot is not touched: only Stella's
interpreter and MCP client code are used, against a Core on a port Stella is not
configured for.
#>
param(
    [Parameter(Mandatory)] [string] $Run,
    [string] $Batch = "field-20261108",
    [int] $Port = 63910,
    [ValidateSet("client", "comes")] [string] $Level = "client"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$work = Join-Path $root ".work\$Batch\$Run"
$reportDir = Join-Path $root "outputs\acceptance\p6-stella-mcp-$Batch"
if ($Level -eq "comes") { $reportDir = "$reportDir-comes" }
$report = Join-Path $reportDir "$Run.json"
if (Test-Path $report) { throw "refusing to overwrite $report" }
New-Item -ItemType Directory -Force $reportDir | Out-Null

$bat = Get-Content (Join-Path (Split-Path -Parent $root) "start_stella.bat")
$line = $bat | Where-Object { $_ -match '"([^"]+python\.exe)"\s+bot\.py' } | Select-Object -First 1
if (-not $line -or $line -notmatch '"([^"]+python\.exe)"') { throw "Stella interpreter not found in start_stella.bat" }
$stellaPython = $Matches[1]
$stellaRoot = ($bat | Where-Object { $_ -match '^cd /d (.+)$' } | Select-Object -First 1) -replace '^cd /d ', ''

$generated = python (Join-Path $root "lab\field\three_host_config.py") --work $work --port $Port | ConvertFrom-Json
$secrets = Get-Content (Join-Path $work "secrets.json") -Raw | ConvertFrom-Json
$cli = Join-Path $root "target\debug\$([char]0x6c)igamina-cli.exe"
$core = Start-Process -FilePath $cli -ArgumentList @("daemon", "--config", (Join-Path $work "daemon.json")) `
    -RedirectStandardOutput (Join-Path $work "core.stdout.log") -RedirectStandardError (Join-Path $work "core.stderr.log") `
    -WindowStyle Hidden -PassThru
$core.Id | Out-File (Join-Path $work "core.pid") -Encoding ascii

$summary = [ordered]@{ run_id = $Run; run_label = $generated.run_label; port = $Port }
try {
    $env:FIELD_MCP_TOKEN = $secrets.mcp_client
    $env:NO_PROXY = "127.0.0.1,localhost"; $env:no_proxy = $env:NO_PROXY
    $env:PYTHONUTF8 = "1"
    $driver = if ($Level -eq "comes") { "stella_comes_experiment.py" } else { "stella_mcp_experiment.py" }
    & $stellaPython -I -X utf8 (Join-Path $root "lab\field\$driver") --stella-root $stellaRoot `
        --mcp-config (Join-Path $work "mcp.toml") --run-id $Run --output $report
    $summary.driver_exit = $LASTEXITCODE
} finally {
    Remove-Item Env:FIELD_MCP_TOKEN -ErrorAction SilentlyContinue
    $code = & curl.exe -s -o NUL -w "%{http_code}" --noproxy "*" -X POST "http://127.0.0.1:$Port/v1/shutdown" -H "authorization: Bearer $($secrets.admin)"
    $summary.shutdown_requested = ($code -eq "202")
    $summary.core_exited = $core.WaitForExit(60000)
    if (-not $summary.core_exited) { & taskkill /T /F /PID $core.Id | Out-Null }
    Start-Sleep -Seconds 3
    $left = & wsl.exe -e docker ps -q --filter "label=io.$([char]0x6c)igamina.field.run=$($generated.run_label)"
    $summary.containers_left = @($left | Where-Object { $_ }).Count
}
$db = Join-Path $work "core.sqlite3"
$summary.gateway_operations = [int](python -c "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('select count(*) from operation_ledger').fetchone()[0])" $db)
$teardown = (($summary | ConvertTo-Json) -replace "`r`n", "`n") + "`n"
[IO.File]::WriteAllText((Join-Path $reportDir "$Run.teardown.json"), $teardown, (New-Object Text.UTF8Encoding $false))
$summary | ConvertTo-Json
