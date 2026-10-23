@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
py -3.13 -I "%~dp0pretrade_launcher.py"
if errorlevel 1 (
  echo Launcher failed. Python 3.13 x64 with Tcl/Tk is required.
  echo No trading service was started. Keep the extracted folder together.
  pause
)
endlocal
