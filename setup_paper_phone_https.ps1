param(
    [string]$Root = (Join-Path $env:LOCALAPPDATA 'AI_IMPULS_PAPER'),
    [string]$ExpectedSid = ''
)
# No firewall or service actions here. The GUI confirms each mutation separately.
$ErrorActionPreference = 'Stop'
if ($Root -match '["\r\n]' -or -not [IO.Path]::IsPathRooted($Root)) { throw 'Invalid PAPER root' }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$currentSid = $identity.User.Value
if ([string]::IsNullOrEmpty($ExpectedSid)) { $ExpectedSid = $currentSid }
if ($ExpectedSid -notmatch '^S-1-[0-9]+(-[0-9]+)+$') { throw 'Invalid expected Windows user SID' }
if ($currentSid -ne $ExpectedSid) {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show('PAPER HTTPS must run as Administrator using the SAME Windows user that launched this wizard. UAC selected a different user. Nothing was changed. Sign in as the original PAPER owner and elevate that account.', 'PAPER HTTPS: Windows user mismatch') | Out-Null
    exit 1
}
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    # Preserve the original PAPER root and reject a different Windows identity after UAC.
    $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $PSCommandPath + '"'), '-Root', ('"' + $Root.TrimEnd('\') + '"'), '-ExpectedSid', $ExpectedSid)
    $child = Start-Process powershell.exe -Verb RunAs -ArgumentList $arguments -Wait -PassThru
    exit $child.ExitCode
}
Set-Location -LiteralPath $PSScriptRoot
& py -3.13 (Join-Path $PSScriptRoot 'paper_https_setup.py') --root $Root
exit $LASTEXITCODE
