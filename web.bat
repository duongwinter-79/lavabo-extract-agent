@echo off
REM Double-click me for the browser version.
REM From a terminal, `web.bat --lan` also serves it to a phone on the same wifi.
cd /d "%~dp0"

if not exist ".venv" (
    echo Dang cai dat lan dau, mat khoang mot phut...
    powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 >nul
    REM Re-run visibly on failure: a hidden setup error otherwise surfaces as the
    REM missing-python.exe line below, which says nothing about the real cause.
    if errorlevel 1 powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
)

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo Cai dat chua xong - chay lai: powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
    pause
    exit /b 1
)

.venv\Scripts\python.exe scripts\lavabo_web.py %*
pause
