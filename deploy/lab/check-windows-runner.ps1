param()

$ErrorActionPreference = "Stop"
$runnerPath = Join-Path $PSScriptRoot "windows/runner.ps1"
$tokens = $null
$parseErrors = $null
[System.Management.Automation.Language.Parser]::ParseFile($runnerPath, [ref]$tokens, [ref]$parseErrors) | Out-Null
if ($parseErrors.Count -gt 0) {
    throw ($parseErrors | ForEach-Object { $_.Message } | Join-String -Separator "; ")
}

$source = Get-Content -LiteralPath $runnerPath -Raw
$marker = $source.IndexOf("jobRunnerSource = @", [StringComparison]::Ordinal)
if ($marker -lt 0) { throw "Windows worker C# source block was not found." }
$bodyStart = $source.IndexOf("`n", $marker, [StringComparison]::Ordinal) + 1
$bodyEnd = $source.IndexOf("'@", $bodyStart, [StringComparison]::Ordinal)
if ($bodyStart -le 0 -or $bodyEnd -le $bodyStart) { throw "Windows worker C# source block is malformed." }
$jobRunnerSource = $source.Substring($bodyStart, $bodyEnd - $bodyStart)
Add-Type -TypeDefinition $jobRunnerSource -ErrorAction Stop
Write-Output "Windows worker PowerShell parsed and Job Object source compiled."
