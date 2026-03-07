@echo off
title YT Automation Studio — Port 5001
cd /d "%~dp0"

echo.
echo  ================================================
echo   YT Automation Studio
echo   http://localhost:5001
echo  ================================================
echo.

:: Check for Python
where python >nul 2>&1
if %errorlevel% neq 0 (
    where py >nul 2>&1
    if %errorlevel% neq 0 (
        echo  [ERROR] Python not found. Please install Python 3.10+
        pause
        exit /b 1
    )
    set PYTHON=py -3
) else (
    set PYTHON=python
)

:: Install / verify dependencies quietly
echo  Checking dependencies...
%PYTHON% -m pip install -r requirements.txt -q --disable-pip-version-check

echo  Starting server...
echo.
%PYTHON% app.py

echo.
echo  Server stopped.
pause
