# Compilation only. No terminal start, EA attachment, network calls or orders.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$MetaEditor,
    [Parameter(Mandatory=$true)][string]$Archive,
    [Parameter(Mandatory=$true)][ValidatePattern('^[a-f0-9]{40}$')][string]$Commit,
    [Parameter(Mandatory=$true)][ValidatePattern('^[a-f0-9]{64}$')][string]$ArchiveSha256,
    [Parameter(Mandatory=$true)][string]$Results,
    [ValidateSet('diagnostic','candidate')][string]$Mode='diagnostic'
)
$ErrorActionPreference = 'Stop'
$launcher = Get-Command py.exe -CommandType Application
& $launcher.Source -3 (Join-Path $PSScriptRoot 'source_build.py') compile --editor $MetaEditor --archive $Archive --commit $Commit --archive-sha256 $ArchiveSha256 --results $Results --mode $Mode
exit $LASTEXITCODE
