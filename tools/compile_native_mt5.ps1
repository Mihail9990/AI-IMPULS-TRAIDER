param([Parameter(Mandatory=$true)][string]$MetaEditor,
      [string]$OutputDirectory = (Join-Path $PSScriptRoot '..\native-build'))
$ErrorActionPreference = 'Stop'
if (!(Test-Path -LiteralPath $MetaEditor -PathType Leaf)) { throw 'Provide the installed MetaEditor64.exe path.' }
$source = Join-Path $PSScriptRoot '..\mt5\native'
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
Copy-Item -Path (Join-Path $source '*.mqh') -Destination $OutputDirectory
Copy-Item -Path (Join-Path $source '*.mq5') -Destination $OutputDirectory
foreach ($name in @('ImpulsTrader','ImpulsObserver','ImpulsSLMonitor')) {
    $file = (Resolve-Path (Join-Path $OutputDirectory ($name+'.mq5'))).Path
    $log = [IO.Path]::GetFullPath((Join-Path $OutputDirectory ($name+'.compile.log')))
    $binary = Join-Path $OutputDirectory ($name+'.ex5')
    if (Test-Path -LiteralPath $binary) { Remove-Item -LiteralPath $binary }
    if (Test-Path -LiteralPath $log) { Remove-Item -LiteralPath $log }
    $process = Start-Process -FilePath $MetaEditor -ArgumentList @("/compile:`"$file`"", "/log:`"$log`"") -Wait -PassThru
    if (!(Test-Path -LiteralPath $log) -or !(Test-Path -LiteralPath $binary)) { throw "No successful build artifacts for $name. Inspect $log" }
    $text = Get-Content -LiteralPath $log -Raw
    if ($text -notmatch '0 errors, 0 warnings') { throw "Compilation requires review: $log" }
    Write-Output "$name compiled with 0 errors and 0 warnings."
}
Write-Output 'Only build files were created. No terminal, EA installation or trading was started.'
