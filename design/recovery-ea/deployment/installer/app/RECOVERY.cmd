@echo off
setlocal DisableDelayedExpansion
if not exist "%~dp0python\python.exe" (
  echo Embedded Python is missing. Repair the RECOVERY installation.
  pause
  exit /b 2
)
"%~dp0python\python.exe" -I -B -X utf8 "%~dp0recovery_setup.py" %*
set "RECOVERY_EXIT=%ERRORLEVEL%"
if "%~1"=="" pause
exit /b %RECOVERY_EXIT%
