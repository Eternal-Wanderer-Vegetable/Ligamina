param([ValidateSet('docs', 'full')][string]$Mode = 'docs')
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    & node scripts/check-docs.mjs
    if ($LASTEXITCODE -ne 0) { throw 'Documentation verification failed.' }
    if ($Mode -eq 'full') {
        & pwsh -NoProfile -File scripts/check.ps1
        if ($LASTEXITCODE -ne 0) { throw 'Runtime verification failed.' }
    }
}
finally { Pop-Location }
