@echo off
setlocal

set SCRIPT_DIR=%~dp0
set SCRIPT=%SCRIPT_DIR%seestar_alpaca.py

where python >nul 2>nul
if %errorlevel%==0 (
    python "%SCRIPT%"
    goto :end
)

where py >nul 2>nul
if %errorlevel%==0 (
    py "%SCRIPT%"
    goto :end
)

echo Could not find Python on this system.
echo Install it from https://www.python.org/downloads/ and make sure it's on PATH.
pause
exit /b 1

:end
pause
endlocal
