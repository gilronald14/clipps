@echo off
REM =====================================================================
REM  Shortsmith - fetch from GitHub and set up, in one step.
REM
REM  setup.bat installs the DEPENDENCIES of a copy you already have; it
REM  does not download the program. This script does the other half: it
REM  fetches the code, then hands over to setup.bat.
REM
REM  Use it with no arguments (it will use the URL below), or pass one:
REM     install.bat https://github.com/you/shortsmith.git
REM =====================================================================
setlocal
title Shortsmith install

REM ---- edit this once, after you publish your repository ----------------
set "REPO=https://github.com/YOUR-USER/YOUR-REPO.git"
REM ----------------------------------------------------------------------

set "DEST=%USERPROFILE%\shortsmith"

if not "%~1"=="" set "REPO=%~1"

echo.
echo   Shortsmith installer
echo   --------------------
echo   repository : %REPO%
echo   install to : %DEST%
echo.

if "%REPO%"=="https://github.com/YOUR-USER/YOUR-REPO.git" (
  echo   This copy still has the placeholder URL in it.
  echo.
  echo   Either edit install.bat and set REPO near the top,
  echo   or run it with the address:
  echo     install.bat https://github.com/you/shortsmith.git
  echo.
  pause
  exit /b 1
)

where git >nul 2>&1
if errorlevel 1 (
  echo   git was not found on PATH.
  echo.
  echo   Either install Git from https://git-scm.com/download/win, or skip
  echo   git entirely: open the repository page, click Code ^> Download ZIP,
  echo   extract it, and run setup.bat inside the extracted folder.
  echo.
  pause
  exit /b 1
)

if exist "%DEST%\.git" (
  echo   Already installed - updating instead.
  git -C "%DEST%" pull --ff-only
  if errorlevel 1 (
    echo.
    echo   Could not update automatically ^(local changes, or no network^).
    echo   Setup will still run against what is already there.
    echo.
  )
) else (
  if exist "%DEST%" (
    echo   %DEST% already exists and is not a git checkout.
    echo   Move it aside first, or install somewhere else.
    echo.
    pause
    exit /b 1
  )
  echo   Cloning...
  git clone "%REPO%" "%DEST%"
  if errorlevel 1 (
    echo.
    echo   Clone failed. Check the address, and that the repo is public or
    echo   that you are signed in to GitHub on this machine.
    echo.
    pause
    exit /b 1
  )
)

echo.
echo   Running setup...
echo.
call "%DEST%\setup.bat"
