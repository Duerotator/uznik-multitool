@echo off
cd /d "%~dp0..\.."
if not exist ".venv\Scripts\python.exe" (
    echo Run setup.bat first to install Uznik MultiTool.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" app\main.py
pause
