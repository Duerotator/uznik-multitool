@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Run setup.bat first to install Uznik MultiTool.
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "%~dp0app\launch.pyw"
