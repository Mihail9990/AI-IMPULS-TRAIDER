@echo off
cd /d "%~dp0"
py -3.13 "%~dp0independent_install.py"
if errorlevel 1 (
  echo Windows Python 3.13 with Tk is required.
  pause
)
