@echo off
setlocal
cd /d "%~dp0"
py -3.12 -I tools\setup_analysis.py %*
if errorlevel 1 (
  echo Analysis setup failed. LIVE Python and trading settings were not changed.
  pause
  exit /b 1
)
echo Optional analysis environment is installed and offline smoke passed.
echo Restart the panel to load it. This does not start or enable trading.
pause
