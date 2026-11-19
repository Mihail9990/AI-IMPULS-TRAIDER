@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 goto launcher
where python >nul 2>nul
if not errorlevel 1 goto python
echo STATUS=PYTHON_NOT_FOUND
goto done
:launcher
py -3 -B "%~dp0check_recovery_acl.py"
if not errorlevel 1 goto done
where python >nul 2>nul
if errorlevel 1 goto done
:python
python -B "%~dp0check_recovery_acl.py"
:done
echo.
pause
