@echo off
REM ==========================================================================
REM  Sister RexenIOT - portable launcher (Windows)
REM
REM  Everything lives inside this folder: the Python runtime, the packages,
REM  the voice models, your settings and your chat history. Copy the folder to
REM  a USB stick and it runs on any Windows machine with a network connection.
REM
REM  The first run downloads a standalone Python (~30 MB) and the packages
REM  (~300 MB). Set REXENIOT_OFFLINE=1 to run with no network afterwards.
REM
REM  Only the part that differs from start.cmd is here: obtaining a Python that
REM  the host may not have. The startup pipeline itself is scripts\bootstrap.py,
REM  shared with the other three launchers, so the two Windows entry points
REM  cannot drift apart.
REM ==========================================================================

setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "APP_DIR=%CD%"
set "RUNTIME=%APP_DIR%\runtime"
set "PYROOT=%RUNTIME%\python"
set "PY=%PYROOT%\python.exe"
set "VENV=%RUNTIME%\venv"
set "VPY=%VENV%\Scripts\python.exe"
set "MARK=%RUNTIME%\.deps-hash"
if not defined REXENIOT_PORT set "REXENIOT_PORT=8060"

REM Keep every byte of state inside the folder.
set "REXENIOT_HOME=%APP_DIR%"
set "REXENIOT_PORTABLE=1"
set "HF_HOME=%APP_DIR%\data\models\hf"
set "HF_HUB_CACHE=%APP_DIR%\data\models\hf\hub"
set "XDG_CACHE_HOME=%APP_DIR%\data\cache"
set "PIP_CACHE_DIR=%RUNTIME%\pip-cache"

if not exist "%RUNTIME%" mkdir "%RUNTIME%"
if not exist "%APP_DIR%\data\models" mkdir "%APP_DIR%\data\models"
if not exist "%APP_DIR%\data\cache" mkdir "%APP_DIR%\data\cache"

REM ------------------------------------------------------------- runtime ----
echo Locating a Python runtime...

set "USE_PY="

if exist "%PY%" (
  set "USE_PY=%PY%"
  echo       using the bundled runtime
  goto :have_python
)

REM Prefer a suitable interpreter already on this machine.
where py >nul 2>&1
if %ERRORLEVEL%==0 (
  py -3 -c "import sys; sys.exit(0 if sys.version_info[:2]>=(3,10) else 1)" >nul 2>&1
  if !ERRORLEVEL!==0 (
    for /f "delims=" %%P in ('py -3 -c "import sys;print(sys.executable)"') do set "USE_PY=%%P"
    echo       using system Python from py launcher
    goto :have_python
  )
)
where python >nul 2>&1
if %ERRORLEVEL%==0 (
  python -c "import sys; sys.exit(0 if sys.version_info[:2]>=(3,10) else 1)" >nul 2>&1
  if !ERRORLEVEL!==0 (
    for /f "delims=" %%P in ('python -c "import sys;print(sys.executable)"') do set "USE_PY=%%P"
    echo       using system Python from PATH
    goto :have_python
  )
)

if "%REXENIOT_OFFLINE%"=="1" (
  echo   ERROR: no suitable Python found and REXENIOT_OFFLINE=1.
  echo   Run once with a network connection, or install Python 3.10+.
  pause
  exit /b 1
)

echo       no suitable Python found; fetching a portable one ^(~30 MB^) ...
set "TARBALL=cpython-3.12.7+20241016-x86_64-pc-windows-msvc-shared-install_only.tar.gz"
set "URL=https://github.com/astral-sh/python-build-standalone/releases/download/20241016/%TARBALL%"
REM Do not name this TMP: that is a Windows built-in pointing at the
REM user's temp directory, and overwriting it breaks tar, pip and anything
REM else that needs a temp path. The caret continuation is also gone -- a
REM trailing space after a caret silently breaks the command.
set "PYTAR=%RUNTIME%\%TARBALL%"

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri '%URL%' -OutFile '%PYTAR%'"
if not exist "%PYTAR%" (
  echo   ERROR: could not download Python. Check the network connection.
  pause
  exit /b 1
)

tar -xzf "%PYTAR%" -C "%RUNTIME%"
if errorlevel 1 (
  echo   ERROR: could not unpack Python. Windows 10 1803+ is required for tar.
  pause
  exit /b 1
)
del "%PYTAR%"

if not exist "%PY%" (
  echo   ERROR: unexpected archive layout; %PY% is missing.
  pause
  exit /b 1
)
set "USE_PY=%PY%"
echo       installed the portable runtime

:have_python

REM ------------------------------------------------------------------ hand ---
REM Steps 2 to 6 are bootstrap.py's: the environment, the packages, the GPU note,
REM the speech warm-up and the server. It re-prints the banner itself.
REM REXENIOT_OFFLINE is already exported above, so it needs no flag here.
"%USE_PY%" "%APP_DIR%\scripts\bootstrap.py" ^
  --venv "%VENV%" ^
  --req "%APP_DIR%\requirements.txt" ^
  --mark "%MARK%" ^
  --port %REXENIOT_PORT% ^
  --host localhost ^
  --bind 127.0.0.1 ^
  --folder "%APP_DIR%" ^
  --portable
set "CODE=%ERRORLEVEL%"

echo.
if not "%CODE%"=="0" echo   Sister RexenIOT exited with code %CODE%.
pause
endlocal
exit /b %CODE%
