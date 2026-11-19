[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$ServerRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ServerRoot '.venv-simulation\Scripts\python.exe'
if (-not (Test-Path $Python)) { throw 'Run Install-Simulation.ps1 first.' }
if ($env:WIDGET_LIVE -and $env:WIDGET_LIVE -ne '0') { throw 'Live mode is not supported. Remove WIDGET_LIVE before using this simulation launcher.' }
$StateDirectory = Join-Path $ServerRoot 'simulation-data'
New-Item -ItemType Directory -Path $StateDirectory -Force | Out-Null
# No token in command-line arguments or configuration files.
$Secret = Read-Host 'Enter the existing widget key (at least 32 characters)' -AsSecureString
$Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secret)
$PreviousToken = $env:WIDGET_TOKEN
$PreviousJournal = $env:RECOVERY_SIM_JOURNAL
try {
    $env:WIDGET_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer)
    if ($env:WIDGET_TOKEN -cnotmatch '^[A-Za-z0-9_-]{32,}$') { throw 'Key must contain at least 32 ASCII letters, digits, underscores or hyphens; use a random key.' }
    $env:RECOVERY_SIM_JOURNAL = Join-Path $StateDirectory 'recovery-simulation.sqlite3'
    Write-Host 'SIMULATION ONLY. Listening on 127.0.0.1:8767; no MT5 connection. Ctrl+C stops it.'
    & $Python (Join-Path $ServerRoot 'run_recovery_sim.py')
    if ($LASTEXITCODE -ne 0) { throw "Simulation exited with code $LASTEXITCODE" }
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer)
    $env:WIDGET_TOKEN = $PreviousToken
    $env:RECOVERY_SIM_JOURNAL = $PreviousJournal
    $Secret.Dispose()
}
