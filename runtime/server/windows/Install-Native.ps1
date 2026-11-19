# Explicit operator action only. Does not initialize MT5 or start trading.
[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$PythonExe)
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Windows required.' }
if (-not [IO.Path]::IsPathRooted($PythonExe) -or -not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) { throw 'Provide the absolute path to a supported 64-bit Python executable.' }
$Root = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Root '.venv-native'
& $PythonExe -c "import sys,struct; assert sys.platform == 'win32' and struct.calcsize('P') == 8 and sys.version_info >= (3,10)"
if ($LASTEXITCODE -ne 0) { throw '64-bit Windows Python 3.10 or newer required; verify MT5 wheel compatibility.' }
$Python = Join-Path $Venv 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) {
    & $PythonExe -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw 'venv creation failed.' }
}
& $Python -m pip install --only-binary=:all: -r (Join-Path $Root 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Do not start runtime.' }
& $Python -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependency consistency check failed.' }
& $Python -m compileall -q (Join-Path $Root 'recovery') (Join-Path $Root 'run_recovery_native.py')
if ($LASTEXITCODE -ne 0) { throw 'Python compilation failed.' }
& $Python -c "import MetaTrader5, fastapi, uvicorn; print('Imports OK; terminal was NOT initialized')"
if ($LASTEXITCODE -ne 0) { throw 'Native imports failed.' }
$InstalledVersions = & $Python -m pip freeze
if ($LASTEXITCODE -ne 0) { throw 'Dependency manifest generation failed.' }
$InstalledVersions | Set-Content -LiteralPath (Join-Path $Venv 'installed-versions.txt') -Encoding ASCII
Write-Host 'Dependencies prepared only. No MT5 connection, EA compilation, firewall, scheduled task or service changes.'
