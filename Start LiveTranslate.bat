@echo off
rem Double-clickable Windows launcher for the LiveTranslate operator control panel.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo First run: creating Python environment (one-time, ~1 minute)...
  py -3 -m venv .venv || python -m venv .venv
  .venv\Scripts\pip install --quiet -e .[dev]
)
.venv\Scripts\python -m livetranslate.control
pause
