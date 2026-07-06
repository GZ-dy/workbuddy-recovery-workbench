@echo off
setlocal
title WorkBuddy Restart 8123
cd /d "%~dp0"

echo [WorkBuddy] Restarting AI Fault Recovery Workbench
echo [WorkBuddy] URL: http://127.0.0.1:8123/
echo.

set "PORT_PID="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:"127.0.0.1:8123 .*LISTENING"') do set "PORT_PID=%%P"

if defined PORT_PID (
  echo [WorkBuddy] Stopping existing service. PID: %PORT_PID%
  taskkill /PID %PORT_PID% /F >nul 2>nul
  if errorlevel 1 (
    echo [ERROR] Failed to stop PID: %PORT_PID%
    pause
    exit /b 1
  )
  timeout /t 2 /nobreak >nul
) else (
  echo [WorkBuddy] No existing service found on port 8123.
)

call "%~dp0start_workbench_8123.bat"
