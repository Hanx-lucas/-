@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Please run the first-time installer before starting the tool.
  pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" "%~dp0baijiu_price_automation\ui_server.py"
pause
