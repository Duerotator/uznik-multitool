@echo off
cd /d "%~dp0..\.."
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\create_shortcut.ps1"
if errorlevel 1 (
    echo Could not create the shortcut. Run setup.bat first.
    pause
    exit /b 1
)
echo Desktop shortcut created.
pause
