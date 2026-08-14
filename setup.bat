@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo   One AI - setup
echo   ==============
echo.

REM ---------------------------------------------------------------- Python ---
REM The 'py' launcher ships with the official python.org installer and is the
REM most reliable way to find Python on Windows. Fall back to 'python', which
REM on a machine without Python opens the Microsoft Store instead of erroring,
REM so we verify it actually runs.
set PY=
py -3 --version >nul 2>&1 && set PY=py -3
if "!PY!"=="" (
    python --version >nul 2>&1 && set PY=python
)

if "!PY!"=="" (
    echo   [X] Python is not installed.
    echo.
    echo   Install it from https://www.python.org/downloads/
    echo   IMPORTANT: tick "Add python.exe to PATH" on the first screen,
    echo   then run this file again.
    echo.
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('!PY! --version 2^>^&1') do set PYVER=%%v
echo   [1/5] Found Python !PYVER!

!PY! -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)"
if errorlevel 1 (
    echo   [X] Python 3.9 or newer is required. You have !PYVER!.
    echo       Install a newer version from https://www.python.org/downloads/
    pause
    exit /b 1
)

REM ------------------------------------------------------------------ venv ---
if not exist ".venv" (
    echo   [2/5] Creating virtual environment...
    !PY! -m venv .venv
    if errorlevel 1 (
        echo   [X] Could not create the virtual environment.
        pause
        exit /b 1
    )
) else (
    echo   [2/5] Virtual environment already exists, reusing it.
)

set VPY=.venv\Scripts\python.exe
if not exist "!VPY!" (
    echo   [X] The virtual environment looks broken. Delete the .venv folder and retry.
    pause
    exit /b 1
)

REM -------------------------------------------------------------- packages ---
echo   [3/5] Installing packages, this takes a minute...
"!VPY!" -m pip install --upgrade pip --quiet
"!VPY!" -m pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo   [X] Package install failed. Check your internet connection and retry.
    pause
    exit /b 1
)

REM ------------------------------------------------------------------ .env ---
if exist ".env" (
    echo   [4/5] .env already exists, leaving it alone.
) else (
    echo   [4/5] Creating .env with a fresh SECRET_KEY...
    "!VPY!" tools\make_env.py
    if errorlevel 1 (
        echo   [X] Could not create .env.
        pause
        exit /b 1
    )
)

REM ----------------------------------------------------------------- check ---
echo   [5/5] Checking the install...
"!VPY!" tools\doctor.py
echo.
echo   Setup finished.
echo.
echo   NEXT: open .env in Notepad and paste your OpenRouter key,
echo         then double-click run.bat
echo.
echo   Get a free key at https://openrouter.ai/keys
echo.

choice /c YN /n /m "   Open .env now? [Y/N] "
if errorlevel 2 goto done
start notepad .env

:done
echo.
pause
