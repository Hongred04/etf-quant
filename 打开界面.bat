@echo off
rem ============================================================
rem  etf-quant web console launcher (ASCII only)
rem  Finds a usable Python 3, starts server.py, opens browser
rem ============================================================
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion
cd /d "%~dp0"
title etf-quant web

set "PY="

if exist "%~dp0.venv\Scripts\python.exe" (
    rem A copied/moved project keeps .venv but its pyvenv.cfg points at the old
    rem location, so the interpreter exists yet cannot start. Verify first.
    "%~dp0.venv\Scripts\python.exe" -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY=%~dp0.venv\Scripts\python.exe"
)
if defined PY goto :found

if exist "%USERPROFILE%\.workbuddy\binaries\python\versions" (
    for /f "delims=" %%v in ('dir /b /o-n "%USERPROFILE%\.workbuddy\binaries\python\versions" 2^>nul') do (
        if not defined PY (
            if exist "%USERPROFILE%\.workbuddy\binaries\python\versions\%%v\python.exe" (
                set "PY=%USERPROFILE%\.workbuddy\binaries\python\versions\%%v\python.exe"
            )
        )
    )
)
if defined PY goto :found

for /f "delims=" %%p in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do (
    if not defined PY set "PY=%%p"
)
if defined PY goto :found

for /f "delims=" %%p in ('where python 2^>nul') do (
    if not defined PY (
        echo %%p | findstr /i "WindowsApps" >nul
        if errorlevel 1 set "PY=%%p"
    )
)
if defined PY goto :found

for %%d in (
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
    "C:\Python313\python.exe"
    "C:\Python312\python.exe"
) do (
    if not defined PY if exist %%d set "PY=%%~d"
)
if defined PY goto :found

goto :nopython

:found
echo  Using Python: %PY%
echo.

rem --- dependency check: auto-install once if pandas/numpy are missing ---
"%PY%" -c "import pandas, numpy" >nul 2>&1
if not errorlevel 1 goto :run

echo  [SETUP] pandas / numpy not found in this Python.
echo  [SETUP] Installing dependencies, this may take a minute...
echo.
"%PY%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 goto :depfail
echo.
echo  [SETUP] Done.
echo.

:run
rem -u keeps stdout unbuffered: otherwise the startup banner sits in Python's
rem 4KB buffer and the user sees an empty window until something else flushes.
"%PY%" -u server.py
set "RC=!ERRORLEVEL!"
if "!RC!"=="0" exit /b 0
if "!RC!"=="2" goto :depfail
rem 130 / 143 = Ctrl+C or window closed, not an error worth a pause prompt
if "!RC!"=="130" exit /b 0
if "!RC!"=="143" exit /b 0
echo.
echo  [ERROR] server exited with code !RC!
echo.
pause
exit /b 0

:depfail
echo.
echo  [ERROR] Dependency install failed.
echo          The most common causes:
echo            1. No network access to pypi.org
echo            2. This Python is a managed/vendor copy that got reset
echo.
echo  Manual fix:
echo          "%PY%" -m pip install -r requirements.txt
echo.
echo  Or delete the .venv folder and run this file again to rebuild it.
echo.
pause
exit /b 1

:nopython
echo.
echo  [ERROR] No usable Python 3 found. Please install Python 3.10+
echo  from https://www.python.org/downloads/ and tick
echo  "Add python.exe to PATH" during setup, then run again.
echo.
pause
exit /b 1
