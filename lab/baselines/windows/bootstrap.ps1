$ErrorActionPreference = 'Stop'
$source = 'C:\Ligamina\Input\project'
$project = 'C:\Ligamina\Workspace\project'
New-Item -ItemType Directory -Path (Split-Path $project) -Force | Out-Null
Copy-Item -LiteralPath $source -Destination $project -Recurse -Force
Set-Location -LiteralPath $project
$python = Join-Path $project 'runtime\python\python.exe'
$runner = Join-Path $project 'lab\baselines\paired_worker.py'
$payload = Join-Path $project 'sample\payload.json'
& $python $runner --payload $payload --timeout 180
exit $LASTEXITCODE
