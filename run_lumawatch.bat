@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" "screen_dimmer.py"
) else (
    start "" pythonw "screen_dimmer.py"
)
