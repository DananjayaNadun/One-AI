@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo   Setup has not been run yet. Double-click setup.bat first.
    echo.
    pause
    exit /b 1
)

echo.
echo   One AI is starting...
echo   Open http://127.0.0.1:5000 in your browser.
echo   Press Ctrl+C in this window to stop it.
echo.

REM Give Flask a moment to bind the port before the browser opens, otherwise
REM the first request lands on a closed socket and shows a connection error.
start "" /b cmd /c "timeout /t 3 >nul & start http://127.0.0.1:5000"

.venv\Scripts\python.exe app.py

echo.
echo   One AI has stopped.
pause
