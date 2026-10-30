@echo off
REM ---------------------------------------------------------------------
REM  Shortsmith - drag a video file onto this icon and drop it.
REM  Progress prints here; when it finishes the shorts open in
REM  %USERPROFILE%\Videos\shorts\<video name>\
REM
REM  Everything runs locally: no upload, no account, no API key.
REM  First time on a new PC? run setup.bat once.
REM ---------------------------------------------------------------------
setlocal enabledelayedexpansion
title Shortsmith
cd /d "%~dp0"

if "%~1"=="" (
  echo.
  echo   Drag a video file onto this icon and drop it.
  echo.
  echo   Or double-click Shortsmith_App.bat for a window with options.
  echo   First time on this PC? run setup.bat once.
  echo.
  pause
  exit /b 1
)

REM Find a Python that actually HAS the libraries. Two stages, because PATH
REM order is not trustworthy: a Windows Store python.exe stub shadows real
REM installs in a normal shell, and a bare "python" may be a fresh install
REM with none of the packages.
REM
REM   1) the interpreter doctor.py recorded after verifying this machine
REM   2) otherwise search, testing for the libraries rather than guessing
set "PY="
set "PYCAND="
if exist "%~dp0python_path.txt" set /p PYCAND=<"%~dp0python_path.txt"
set PYCAND=!PYCAND:"=!
REM Go through :try so the value is quoted exactly like the fallbacks below.
REM This matters: plenty of Windows setups have a space in the user profile
REM path ("C:\Users\First Last\..."), and an unquoted interpreter path splits
REM at that space - Python then runs with a mangled filename.
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
  echo.
  echo   Run setup.bat once and it will sort that out.
  echo   Or, by hand:
  echo     python -m pip install mediapipe faster-whisper opencv-python pillow numpy sherpa-onnx
  echo.
  pause
  exit /b 1
)

echo.
echo   Shortsmith
echo   Input : %~nx1
echo   Using : !PY!
echo.

!PY! "%~dp0make_shorts.py" "%~1" --clips 7 --open
set "RC=!ERRORLEVEL!"

echo.
if "!RC!"=="0" (
  echo   Done. Shorts are in %USERPROFILE%\Videos\shorts\
) else (
  echo   Stopped with code !RC! - the messages above say why.
)
echo.
pause
exit /b !RC!

:try
REM %* is the candidate command. Test for the libraries, not just for a
REM Python: any interpreter that cannot import them is useless here.
if defined PY goto :eof
%* -c "import mediapipe,faster_whisper,cv2" >nul 2>&1
if not errorlevel 1 set "PY=%*"
goto :eof
