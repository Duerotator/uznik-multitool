@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -X utf8 "scripts\sessions\batch_create_sessions.py" %*
set "BATCH_SESSION_EXIT_CODE=%ERRORLEVEL%"
echo.
if not "%BATCH_SESSION_EXIT_CODE%"=="0" echo Batch session creation stopped. Read the message above.
pause
exit /b %BATCH_SESSION_EXIT_CODE%
