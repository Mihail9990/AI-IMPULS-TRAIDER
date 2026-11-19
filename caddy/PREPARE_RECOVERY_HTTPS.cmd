@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0"
py -3.13 -B "%~dp0prepare_caddy.py"
if errorlevel 1 pause
