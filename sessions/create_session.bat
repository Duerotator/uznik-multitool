@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -X utf8 "scripts\sessions\create_session.py" %*
set "CREATE_SESSION_EXIT_CODE=%ERRORLEVEL%"
echo.
if not "%CREATE_SESSION_EXIT_CODE%"=="0" echo Session creation stopped. Read the message above.
pause
exit /b %CREATE_SESSION_EXIT_CODE%
