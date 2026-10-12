@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
if errorlevel 1 goto folder_error
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" "tools\check_mt5.py"
  goto finished
)
py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 (
  py -3 "tools\check_mt5.py"
  goto finished
)
python -c "import sys" >nul 2>&1
if not errorlevel 1 (
  python "tools\check_mt5.py"
  goto finished
)
echo FAIL: Python 3 was not found. Install 64-bit Python manually, then retry.
powershell -NoProfile -Command "$p=Join-Path (Get-Location) ('mt5_check_'+(Get-Date -Format 'yyyyMMdd_HHmmss_fff')+'_'+[guid]::NewGuid().ToString('N')+'.txt'); $f=[IO.File]::Open($p,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write); $w=New-Object IO.StreamWriter($f); $w.WriteLine('FAIL: PYTHON_NOT_FOUND. No MT5 checks ran. execution_ready=false. No trading permissions changed.'); $w.Dispose(); Write-Host ('Report: '+[IO.Path]::GetFileName($p))"
echo Send only the generated report; never tokens, configs or SQLite.
goto finished
:folder_error
echo FAIL: Cannot open the project folder. No checks ran.
:finished
echo.
echo Read-only check finished. Trading was not enabled.
pause
endlocal
