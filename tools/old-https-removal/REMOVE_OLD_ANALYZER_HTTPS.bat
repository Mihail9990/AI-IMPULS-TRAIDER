@echo off
cd /d "%~dp0"
rem No service action occurs before GUI diagnosis and explicit confirmation.
py -3.13 "%~dp0https_setup.py" --remove-old
if errorlevel 1 (
  echo Windows Python 3.13 with Tk is required. Run as Administrator for service changes.
  pause
)
