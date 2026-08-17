@echo off
setlocal

set SCRIPT_DIR=%~dp0
set VENV_PY=%SCRIPT_DIR%.venv\Scripts\python.exe

if not exist "%VENV_PY%" (
    echo Virtual environment not found at %VENV_PY%
    echo Run setup first:
    echo   python -m venv .venv
    echo   .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

"%VENV_PY%" "%SCRIPT_DIR%main.py"
pause
endlocal
