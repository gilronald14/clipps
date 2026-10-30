@echo off
REM ---------------------------------------------------------------------
REM  Shortsmith - opens the window: choose a video, set options, press Start.
REM  Launches with pythonw where possible so no console box sits behind it.
REM  First time on a new PC? run setup.bat once.
REM ---------------------------------------------------------------------
setlocal enabledelayedexpansion
title Shortsmith
cd /d "%~dp0"

REM Same two-stage search as Shortsmith_Drop.bat: use the interpreter that
REM doctor.py verified, else test candidates for the libraries. PATH order
REM alone is not safe on Windows.
set "PY="
set "PYCAND="
if exist "%~dp0python_path.txt" set /p PYCAND=<"%~dp0python_path.txt"
set PYCAND=!PYCAND:"=!
REM Routed through :try so the value is quoted like the fallbacks below - an
REM unquoted path with a space in it (a normal user profile) splits and
REM Python is launched with a mangled filename.
if defined PYCAND call :try "!PYCAND!"

if not defined PY call :try python
if not defined PY call :try py -3
if not defined PY call :try "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PY call :try "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY call :try "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY call :try "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if not defined PY call :try "%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
if not defined PY call :try "C:\Python313\python.exe"
if not defined PY call :try "C:\Python312\python.exe"
if not defined PY call :try "C:\Python311\python.exe"
if not defined PY call :try "C:\Python310\python.exe"
if not defined PY call :try "C:\Python314\python.exe"
if not defined PY call :try "C:\Program Files\Python313\python.exe"
if not defined PY call :try "C:\Program Files\Python312\python.exe"
if not defined PY call :try "C:\Program Files\Python311\python.exe"

if not defined PY (
  echo.
  echo   Could not find a Python with the libraries this needs.
  echo   Run setup.bat once and it will sort that out.
  echo.
  pause
  exit /b 1
)

REM Prefer the windowless interpreter sitting beside the one we found, so no
REM console window lingers behind the app.
set "PYW="
for %%D in ("!PY!") do if exist "%%~dpDpythonw.exe" set "PYW=%%~dpDpythonw.exe"

if defined PYW (
  start "Shortsmith" "!PYW!" "%~dp0shortsmith_app.py"
) else (
  start "Shortsmith" !PY! "%~dp0shortsmith_app.py"
)
exit /b 0

:try
if defined PY goto :eof
%* -c "import mediapipe,faster_whisper,cv2" >nul 2>&1
if not errorlevel 1 set "PY=%*"
goto :eof
