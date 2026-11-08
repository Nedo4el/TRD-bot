@echo off
chcp 65001 >nul
cd /d "%~dp0"
set DASH_OPEN_BROWSER=1
set PYTHONIOENCODING=utf-8
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" run_dashboard.py
) else (
    python run_dashboard.py
)
pause
