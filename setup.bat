@echo off
REM ---------------------------------------------------------------------
REM  Shortsmith - one-time setup. Double-click this on a new PC.
REM
REM  Finds a usable Python, installs the libraries, downloads the model
REM  files, then verifies the machine. Safe to run again at any time.
REM
REM  It does NOT install ffmpeg for you - that is a large third-party
REM  binary and a setup script should not fetch one silently. If ffmpeg
REM  is missing the check at the end tells you where to get it.
REM ---------------------------------------------------------------------
setlocal
title Shortsmith setup
cd /d "%~dp0"

echo.
echo   Shortsmith - one-time setup
echo   ---------------------------
echo.

set "PY="

REM Search the usual places. The version test doubles as a filter: the
REM Windows Store "python.exe" alias is a stub that fails it, so simply
REM taking the first "python" on PATH is not safe.
call :try python
call :try py -3
call :try "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
call :try "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
call :try "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
call :try "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
call :try "%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
call :try "C:\Python313\python.exe"
call :try "C:\Python312\python.exe"
call :try "C:\Python311\python.exe"
call :try "C:\Python310\python.exe"
call :try "C:\Python314\python.exe"
call :try "C:\Program Files\Python313\python.exe"
call :try "C:\Program Files\Python312\python.exe"
call :try "C:\Program Files\Python311\python.exe"

if not defined PY (
  echo   Could not find a Python 3.9 or newer on this PC.
  echo.
  echo   1. Download it from https://www.python.org/downloads/windows/
  echo   2. During the install, TICK "Add python.exe to PATH"
  echo   3. Run this setup again
  echo.
  pause
  exit /b 1
)

echo   Using Python: %PY%
echo.

%PY% "%~dp0setup_env.py" %*
set "RC=%ERRORLEVEL%"

echo.
echo   Adding a desktop and Start menu shortcut...
REM Done even if a check failed above: the window still opens, it just
REM reports the same problem, and that is more useful than no icon at all.
%PY% "%~dp0install_shortcuts.py"

echo.
if "%RC%"=="0" (
  echo   Setup finished - you are ready to go.
) else (
  echo   Setup finished with problems ^(code %RC%^).
  echo   Most failures are a missing ffmpeg or no internet for the models.
  echo   Re-running this is safe; finished downloads are kept.
)
echo.
pause
exit /b %RC%

:try
REM %* is the candidate command; skip if we already have one
if defined PY goto :eof
%* -c "import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=%*"
goto :eof
