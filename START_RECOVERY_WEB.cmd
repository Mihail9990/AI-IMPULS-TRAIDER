@echo off
setlocal DisableDelayedExpansion
call "%~dp0runtime\START_SETUP.cmd"
exit /b %errorlevel%
