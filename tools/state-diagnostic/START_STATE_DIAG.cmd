@echo off
setlocal EnableExtensions DisableDelayedExpansion
py -3.13 -I -B -c "import sys; sys.exit(0)" >nul 2>&1
if errorlevel 1 (
  echo {"result":"PYTHON_3_13_LAUNCHER_REQUIRED"}
  echo Python 3.13 with Python Launcher is required. See README_RU.txt.
  pause
  exit /b 1
)
py -3.13 -I -B "%~dp0state_readonly_diag.py" 2>nul
if errorlevel 1 echo {"result":"DIAGNOSTIC_COULD_NOT_RUN"}
echo.
echo Copy or photograph the JSON above. No reset was performed.
pause
endlocal
