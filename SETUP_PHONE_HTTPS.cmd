@echo off
setlocal DisableDelayedExpansion
call "%~dp0caddy\PREPARE_RECOVERY_HTTPS.cmd"
exit /b %errorlevel%
