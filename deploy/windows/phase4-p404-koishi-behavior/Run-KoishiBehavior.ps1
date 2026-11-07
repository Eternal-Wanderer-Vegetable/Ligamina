$ErrorActionPreference = 'Stop'
$inputRoot = 'C:\Ligamina\Input'
$workspaceRoot = 'C:\Ligamina\Workspace'
$project = Join-Path $workspaceRoot 'project'
$runtime = Join-Path $workspaceRoot 'runtime'
Copy-Item -LiteralPath (Join-Path $inputRoot 'project') -Destination $project -Recurse -Force
Copy-Item -LiteralPath (Join-Path $inputRoot 'runtime') -Destination $runtime -Recurse -Force

$node = Join-Path $runtime 'node.exe'
$signature = Get-AuthenticodeSignature -LiteralPath $node
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'OpenJS Foundation') {
  throw 'The staged Node runtime does not have a valid OpenJS Foundation signature.'
}
$version = (& $node --version).Trim()
if ($LASTEXITCODE -ne 0 -or $version -ne 'v24.20.0') {
  throw "Unexpected signed Node runtime version: $version"
}

$npmCache = Join-Path $workspaceRoot 'Temp\npm-cache'
New-Item -ItemType Directory -Path $npmCache -Force | Out-Null
$npmCli = Join-Path $runtime 'node_modules\npm\bin\npm-cli.js'
$env:PATH = "$runtime;$env:PATH"
Push-Location $project
try {
  & $node $npmCli ci --ignore-scripts --offline --no-audit --no-fund --strict-peer-deps --engine-strict --cache $npmCache
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
  Pop-Location
}

$env:NODE_OPTIONS = $null
$env:LIGAMINA_P404_PROJECT = $project
$env:LIGAMINA_P404_WORKSPACE = Join-Path $workspaceRoot 'run'
$env:LIGAMINA_P404_REQUEST = Join-Path $inputRoot 'request.json'
$env:LIGAMINA_P404_RESULT = Join-Path $workspaceRoot 'run\native-result.json'
$env:LIGAMINA_KOISHI_DATA_DIR = Join-Path $workspaceRoot 'run\native-koishi-data'
New-Item -ItemType Directory -Path $env:LIGAMINA_P404_WORKSPACE,$env:LIGAMINA_KOISHI_DATA_DIR -Force | Out-Null
& $node (Join-Path $inputRoot 'Execute-KoishiBehavior.mjs')
exit $LASTEXITCODE
