@echo off
REM Double-click me on Windows.
cd /d "%~dp0"

if not exist ".venv" (
    echo Dang cai dat lan dau, mat khoang mot phut...
    powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 >nul
    if errorlevel 1 powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
)

.venv\Scripts\python.exe scripts\lavabo_app.py
pause
