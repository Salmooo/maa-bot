@echo off
cd /d "%~dp0"
where pythonw.exe >nul 2>nul
if errorlevel 1 (
  python run.py gui
) else (
  start "" pythonw.exe "%~dp0run.py" gui
)
