@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -X utf8 "scripts\sessions\menu.py"
set "CREATE_SESSIONS_EXIT_CODE=%ERRORLEVEL%"
if not "%CREATE_SESSIONS_EXIT_CODE%"=="0" echo Session menu stopped. Read the message above.
pause
exit /b %CREATE_SESSIONS_EXIT_CODE%
