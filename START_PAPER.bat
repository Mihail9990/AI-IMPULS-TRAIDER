@echo off
cd /d "%~dp0"
py -3.13 "%~dp0independent_install.py" --role paper
if errorlevel 1 pause
