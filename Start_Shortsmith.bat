@echo off
REM =====================================================================
REM  Shortsmith - start the local web UI and open it in your browser.
REM
REM  Double-click this. A console window stays open while the site is
REM  running; close it (or press Ctrl+C in it) to stop the site.
REM
REM  It runs on 127.0.0.1 only, so nothing outside this PC can reach it.
REM =====================================================================
setlocal enabledelayedexpansion
title Shortsmith

cd /d "%~dp0"

set "PY="

REM Test each candidate for the libraries the renderer needs, rather than
REM trusting the first "python" on PATH - a Windows Store python.exe stub
REM shadows real installs and would fail later, at the worst moment.
call :try python
call :try py -3
call :try "C:\Python314\python.exe"
call :try "C:\Python313\python.exe"
call :try "C:\Python312\python.exe"
call :try "C:\Python311\python.exe"
call :try "%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
call :try "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
call :try "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
call :try "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
call :try "C:\Program Files\Python313\python.exe"
call :try "C:\Program Files\Python312\python.exe"

if not defined PY (
  echo.
  echo   Could not find a Python with the libraries this needs.
  echo   Run setup.bat once and it will sort that out.
  echo.
  pause
  exit /b 1
)

echo   Starting Shortsmith on http://127.0.0.1:8765/
echo   Your browser should open by itself.
echo   Leave this window open; press Ctrl+C here to stop.
echo.
%PY% "%~dp0webapp.py"
echo.
echo   Server stopped.
pause
exit /b 0

:try
REM %* is the candidate command; skip if we already have one
if defined PY goto :eof
%* -c "import mediapipe,faster_whisper,cv2" >nul 2>&1
if not errorlevel 1 set "PY=%*"
goto :eof
