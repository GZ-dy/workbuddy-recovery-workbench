@echo off
setlocal
title WorkBuddy Workbench 8123
cd /d "%~dp0"

if not exist "runtime_data" mkdir "runtime_data"

set "PYTHONIOENCODING=utf-8"
set "WORKBENCH_DB=%CD%\runtime_data\workbench.db"
set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"
if exist "%CD%\mail_config.local.bat" call "%CD%\mail_config.local.bat"

echo [WorkBuddy] Starting AI Fault Recovery Workbench
echo [WorkBuddy] URL: http://127.0.0.1:8123/
echo [WorkBuddy] DB: %WORKBENCH_DB%
echo [WorkBuddy] Keep this window open. Closing it stops the service.
echo.

set "PORT_PID="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:"127.0.0.1:8123 .*LISTENING"') do set "PORT_PID=%%P"
if defined PORT_PID (
  echo [WorkBuddy] Port 8123 is already running. PID: %PORT_PID%
  echo [WorkBuddy] Open: http://127.0.0.1:8123/
  pause
  exit /b 0
)

if not exist "%PYTHON_EXE%" (
  echo [ERROR] Python not found: %PYTHON_EXE%
  pause
  exit /b 1
)

"%PYTHON_EXE%" -m uvicorn app:app --host 127.0.0.1 --port 8123

set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo [ERROR] Service exited with code: %EXIT_CODE%
echo [ERROR] If you see "address already in use", port 8123 is already running.
pause
exit /b %EXIT_CODE%
