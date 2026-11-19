# This starts the native dispatcher and can resume real broker actions.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Terminal,
    [Parameter(Mandatory=$true)][long]$Account,
    [Parameter(Mandatory=$true)][string]$BrokerServer,
    [Parameter(Mandatory=$true)][string]$Symbol,
    [Parameter(Mandatory=$true)][long]$Magic,
    [Parameter(Mandatory=$true)][string]$Database,
    [switch]$RunNative
)
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Windows required.' }
if (-not $RunNative) { throw 'Explicit -RunNative required. Startup can resume persisted live ranges.' }
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root '.venv-native\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw 'Run Install-Native.ps1 first.' }
if (-not [IO.Path]::IsPathRooted($Terminal) -or -not (Test-Path -LiteralPath $Terminal -PathType Leaf)) { throw 'Absolute existing terminal executable required.' }
if (-not [IO.Path]::IsPathRooted($Database)) { throw 'Absolute persistent database path required.' }
$StateDir = Split-Path -Parent $Database
if (-not (Test-Path -LiteralPath $StateDir -PathType Container)) { throw 'Create and restrict the persistent state folder first. See NATIVE_SETUP_RU.md.' }
$Secret = Read-Host 'Existing app token (random 32+ ASCII letters/digits/_/-)' -AsSecureString
$Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secret)
$PreviousToken = $env:RECOVERY_API_TOKEN
try {
    $env:RECOVERY_API_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer)
    if ($env:RECOVERY_API_TOKEN -cnotmatch '^[A-Za-z0-9_-]{32,}$') { throw 'Invalid token format.' }
    Write-Host 'NATIVE: real/demo terminal. Persisted active ranges may resume. Ctrl+C stops Python, NOT broker orders.'
    & $Python (Join-Path $Root 'run_recovery_native.py') --run-native --terminal $Terminal --account $Account --server $BrokerServer --symbol $Symbol --magic $Magic --database $Database --port 8767
    if ($LASTEXITCODE -ne 0) { throw "Native runtime exited with code $LASTEXITCODE. Inspect terminal and journal; do not blindly retry commands." }
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer)
    $env:RECOVERY_API_TOKEN = $PreviousToken
    $Secret.Dispose()
}
