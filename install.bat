@echo off
setlocal
cd /d "%~dp0"

echo ===================================
echo  LumaWatch Installer
echo ===================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found on PATH. Install Python 3.10+ from
    echo         https://python.org and re-run this script.
    pause
    exit /b 1
)

if not exist ".venv" (
    echo Creating virtual environment...
    python -m venv .venv
)

echo Installing dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt

if errorlevel 1 (
    echo.
    echo [ERROR] Dependency install failed. See output above.
    pause
    exit /b 1
)

echo.
echo Install complete.
echo.
echo   To run LumaWatch now:        run_lumawatch.bat
echo   To enable auto-start:        open LumaWatch and check
echo                                 "Start with Windows" in the app.
echo.
pause
