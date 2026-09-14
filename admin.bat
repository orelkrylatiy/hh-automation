@echo off
setlocal

cd /d "%~dp0"
set "CONFIG_DIR=%APPDATA%\hh-applicant-tool"
set "PYTHONUTF8=1"

".venv\Scripts\python.exe" -m uvicorn admin.ui_app:app --reload --host 127.0.0.1 --port 8000
