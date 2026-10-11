@echo off
setlocal
set "HERE=%~dp0"
set "PYTHON=%HERE%..\.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=%HERE%.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
cd /d "%HERE%"
if exist "%PYTHON%" (
  "%PYTHON%" "%HERE%ui_server.py"
) else (
  echo Please run the first-time installer, or install Python dependencies.
  py -3.11 "%HERE%launch_ui.py"
)
pause

