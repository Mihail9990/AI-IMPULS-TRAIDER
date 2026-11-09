@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_paper_phone_https.ps1"
if errorlevel 1 pause
