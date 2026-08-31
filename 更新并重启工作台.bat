@echo off
setlocal
cd /d "%~dp0"

set "START_OPTIONS=-Restart"
if /I "%BACKLOT_NO_BROWSER%"=="1" set "START_OPTIONS=-Restart -NoBrowser"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_backlot.ps1" %START_OPTIONS%
if errorlevel 1 (
  echo.
  pause
)
