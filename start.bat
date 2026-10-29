@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\pythonw.exe" (
    start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0launch.pyw" %*
    if errorlevel 1 goto failed
    exit /b 0
)
where pythonw.exe >nul 2>nul
if not errorlevel 1 (
    start "" pythonw.exe "%~dp0launch.pyw" %*
    if errorlevel 1 goto failed
    exit /b 0
)
where pyw.exe >nul 2>nul
if not errorlevel 1 (
    start "" pyw.exe -3 "%~dp0launch.pyw" %*
    if errorlevel 1 goto failed
    exit /b 0
)
:failed
echo Could not start Whale Friend. Install Python 3.11+ with Tcl/Tk support.
echo See README.md. If a window opened and then failed, check .local\startup.log.
pause
exit /b 1
