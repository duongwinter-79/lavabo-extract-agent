@echo off
cd /d "%~dp0"
if not exist ".venv" powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 >nul
.venv\Scripts\python.exe scripts\lavabo_web.py %*
pause
