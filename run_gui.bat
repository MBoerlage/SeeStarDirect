@echo off
setlocal

set SCRIPT_DIR=%~dp0
set VENV_DIR=%SCRIPT_DIR%.venv
set VENV_PY=%VENV_DIR%\Scripts\python.exe
set VENV_PYW=%VENV_DIR%\Scripts\pythonw.exe

if not exist "%VENV_PY%" (
    echo ============================================================
    echo  Virtual environment not found at:
    echo    %VENV_DIR%
    echo ============================================================
    echo Run setup first, from this folder:
    echo   python -m venv .venv
    echo   .venv\Scripts\pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

rem pythonw.exe has no console, so a crash there is silent -- check
rem dependencies first with the regular (console-attached) python.exe so
rem a missing/broken package gives a visible, actionable error instead of
rem the window just flashing and vanishing.
echo Checking dependencies...
"%VENV_PY%" -c "import requests, numpy, PIL, astropy" 2>nul
if errorlevel 1 (
    echo ============================================================
    echo  A required Python package is missing or broken.
    echo ============================================================
    echo Try reinstalling dependencies:
    echo   .venv\Scripts\pip install -r requirements.txt
    echo.
    echo Detailed error:
    "%VENV_PY%" -c "import requests, numpy, PIL, astropy"
    echo.
    pause
    exit /b 1
)

if exist "%VENV_PYW%" (
    start "" "%VENV_PYW%" "%SCRIPT_DIR%gui.py"
) else (
    start "" "%VENV_PY%" "%SCRIPT_DIR%gui.py"
)

endlocal
