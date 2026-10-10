[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "Programs\Ligamina"),
    [string]$UserDataRoot = (Join-Path $env:APPDATA "Ligamina"),
    [switch]$RemoveUserData
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$installFull = [System.IO.Path]::GetFullPath($InstallRoot).TrimEnd([System.IO.Path]::DirectorySeparatorChar)
$driveRoot = [System.IO.Path]::GetPathRoot($installFull).TrimEnd([System.IO.Path]::DirectorySeparatorChar)
if ($installFull -eq $driveRoot -or (Split-Path -Leaf $installFull) -notlike "Ligamina*") {
    throw "Refusing to remove a path that is not a Ligamina install directory: '$installFull'"
}

if (Test-Path -LiteralPath $installFull) {
    if ($PSCmdlet.ShouldProcess($installFull, "Remove Ligamina application versions")) {
        Remove-Item -LiteralPath $installFull -Recurse -Force
    }
}

if ($RemoveUserData) {
    $dataFull = [System.IO.Path]::GetFullPath($UserDataRoot).TrimEnd([System.IO.Path]::DirectorySeparatorChar)
    if ((Split-Path -Leaf $dataFull) -ne "Ligamina") {
        throw "Refusing to remove a data path not named Ligamina: '$dataFull'"
    }
    if ((Test-Path -LiteralPath $dataFull) -and $PSCmdlet.ShouldProcess($dataFull, "Remove Ligamina user data")) {
        Remove-Item -LiteralPath $dataFull -Recurse -Force
    }
}
elseif (Test-Path -LiteralPath $UserDataRoot) {
    Write-Output "User data preserved at: $UserDataRoot"
}
