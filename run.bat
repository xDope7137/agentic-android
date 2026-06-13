@echo off
REM Agentic Android launcher for Windows.
REM   Double-click to start (uses agentic-android.toml), or from a terminal:
REM     run.bat --list-devices
REM     run.bat --provider openai "Install WhatsApp"
REM First run creates its own virtualenv and installs everything.
setlocal
cd /d "%~dp0"

REM Use a separate venv from the Linux .venv (the folder is shared across OSes).
set "VENV=.venv-win"
set "PY=%VENV%\Scripts\python.exe"

if not exist "%PY%" (
  echo [Agentic Android] First run: creating virtualenv and installing dependencies...
  where py >nul 2>nul
  if %errorlevel%==0 ( py -3 -m venv "%VENV%" ) else ( python -m venv "%VENV%" )
  if not exist "%PY%" (
    echo [Agentic Android] ERROR: could not create a virtualenv.
    echo Install Python 3.10+ from https://www.python.org/downloads/ ^(tick "Add to PATH"^) and run this again.
    pause
    exit /b 1
  )
  "%PY%" -m pip install --upgrade pip
  "%PY%" -m pip install -e .
)

"%PY%" -m agentic_android %*

REM Keep the window open if double-clicked with no arguments.
if "%~1"=="" pause
