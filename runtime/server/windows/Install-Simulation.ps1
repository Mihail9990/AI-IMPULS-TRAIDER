# Run explicitly by the operator; no administrator privileges required.
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$ServerRoot = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $ServerRoot '.venv-simulation'
$Python = Join-Path $Venv 'Scripts\python.exe'
if (-not (Test-Path $Python)) {
    & py -3 -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3 / venv creation failed. Install supported Python from python.org first.' }
}
& $Python -m pip install -r (Join-Path $PSScriptRoot 'requirements-simulation.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed; do not start the application.' }
& $Python -m compileall -q (Join-Path $ServerRoot 'recovery') (Join-Path $ServerRoot 'run_recovery_sim.py')
if ($LASTEXITCODE -ne 0) { throw 'Python compilation failed.' }
Write-Host 'Simulation dependencies installed. No MT5, services, firewall rules or scheduled tasks were installed.'
