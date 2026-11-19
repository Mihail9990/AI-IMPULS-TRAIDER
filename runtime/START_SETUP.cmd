@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
  py -3 "%~dp0setup_gui.py"
  if errorlevel 1 pause
  exit /b
)
where python >nul 2>nul
if not errorlevel 1 (
  python "%~dp0setup_gui.py"
  if errorlevel 1 pause
  exit /b
)
echo Python was not found. Install official Python x64 with tkinter and the launcher from python.org.
echo Then extract the full ZIP and double-click START_SETUP.cmd again.
pause
