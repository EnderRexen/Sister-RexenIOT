@echo off
REM ============================================================================
REM  Sister RexenIOT - Windows launcher
REM
REM  Finds a Python 3.10+ interpreter, then hands the rest of the startup to
REM  scripts\bootstrap.py. Everything after "which Python" -- the virtual
REM  environment, the dependency check, the GPU note, the speech warm-up and the
REM  server -- is shared with the other three launchers and lives in that file,
REM  so this one cannot drift out of step with them.
REM
REM  Safe to run repeatedly: an existing environment and downloaded models are
REM  reused.
REM ============================================================================

setlocal EnableDelayedExpansion
title Sister RexenIOT - starting

cd /d "%~dp0"

if not defined REXENIOT_PORT set "REXENIOT_PORT=8060"

REM ---------------------------------------------------------------- Python ---
echo Locating Python...

set "PYEXE="
where py >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE (
  where python >nul 2>nul && set "PYEXE=python"
)
if not defined PYEXE (
  echo.
  echo   ERROR: Python was not found on PATH.
  echo.
  echo   Install Python 3.10 or newer from https://www.python.org/downloads/
  echo   During install, tick "Add python.exe to PATH".
  echo.
  echo   Or use start-portable.cmd, which needs no Python at all.
  echo.
  pause
  exit /b 1
)

REM The version floor is enforced here as well as in bootstrap.py, because this is
REM the first moment a wrong interpreter can be caught, and the message is more
REM useful than a syntax error from the newer syntax bootstrap.py uses.
%PYEXE% -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3,10) else 1)" >nul 2>nul
if errorlevel 1 (
  echo.
  echo   ERROR: Python 3.10 or newer is required.
  %PYEXE% --version
  echo   Download a current version from https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)

REM ------------------------------------------------------------------ hand ---
REM bootstrap.py prints the banner and the remaining steps. The port is passed in
REM so the launcher and the server cannot disagree about it.
%PYEXE% "scripts\bootstrap.py" ^
  --venv ".venv" ^
  --req "requirements.txt" ^
  --mark ".venv\.req-hash" ^
  --port %REXENIOT_PORT% ^
  --host localhost ^
  --bind 127.0.0.1 ^
  --folder "%CD%"
set "CODE=%ERRORLEVEL%"

echo.
if not "%CODE%"=="0" echo   Sister RexenIOT exited with code %CODE%.
pause
endlocal
exit /b %CODE%
