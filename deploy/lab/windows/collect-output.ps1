$ErrorActionPreference = 'Stop'
$source = 'C:\ProgramData\Ligamina\InternalOutput'
$destination = 'C:\ProgramData\Ligamina\Outbound'

foreach ($name in @('result.json', 'stdout.bin', 'stderr.bin')) {
  $sourcePath = Join-Path $source $name
  if (Test-Path -LiteralPath $sourcePath -PathType Leaf) {
    Copy-Item -LiteralPath $sourcePath -Destination (Join-Path $destination $name) -Force
  }
}

if (-not (Test-Path -LiteralPath (Join-Path $destination 'result.json') -PathType Leaf)) {
  throw 'The trusted worker did not produce a result document.'
}
