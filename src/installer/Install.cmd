@echo off
setlocal
set "REC_PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if exist "%REC_PY%" goto run
where py.exe >nul 2>nul
if errorlevel 1 goto need
for /f "usebackq delims=" %%I in (`py.exe -3.13 -c "import sys;print(sys.executable)" 2^>nul`) do set "REC_PY=%%I"
if not exist "%REC_PY%" goto need
:run
"%REC_PY%" -B -X utf8 "%~dp0install.py" %*
set "REC_EXIT=%ERRORLEVEL%"
if not "%REC_EXIT%"=="0" pause
exit /b %REC_EXIT%
:need
echo Install the full 64-bit CPython 3.13 Windows distribution with Tcl/Tk.
echo Then run Install.cmd again. See README_RU.txt.
pause
exit /b 2
