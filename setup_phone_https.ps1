# Local launcher only. No permanent execution-policy changes or broker commands.
$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'Run SETUP_PHONE_HTTPS.bat as Administrator, using the same Windows user.'
    exit 1
}
Set-Location -LiteralPath $PSScriptRoot
& py -3.13 (Join-Path $PSScriptRoot 'https_simple.py')
exit $LASTEXITCODE
