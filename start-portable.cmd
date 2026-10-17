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

echo.
echo  ==========================================================
echo    S I S T E R   R E X E N I O T   ^(portable^)
echo    keeper of the house
echo  ==========================================================
echo    folder: %APP_DIR%
echo.

if not exist "%RUNTIME%" mkdir "%RUNTIME%"
if not exist "%APP_DIR%\data\models" mkdir "%APP_DIR%\data\models"
if not exist "%APP_DIR%\data\cache" mkdir "%APP_DIR%\data\cache"

REM ------------------------------------------------------------- runtime ----
echo [1/5] Locating a Python runtime...

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

REM ---------------------------------------------------------------- venv ----
echo [2/5] Preparing the environment...

if not exist "%VPY%" (
  "%USE_PY%" -m venv "%VENV%"
  if errorlevel 1 (
    echo   ERROR: could not create a virtual environment.
    pause
    exit /b 1
  )
)

REM ------------------------------------------------------------- packages ---
echo [3/5] Checking packages...

set "REQ=%APP_DIR%\requirements.txt"
for /f "skip=1 tokens=* delims=" %%H in ('certutil -hashfile "%REQ%" SHA256') do (
  if not defined HASH set "HASH=%%H"
)
set "HASH=%HASH: =%"

set "NEED_INSTALL=1"
if exist "%MARK%" (
  set /p OLDHASH=<"%MARK%"
  if "!OLDHASH!"=="!HASH!" set "NEED_INSTALL=0"
)

if "%NEED_INSTALL%"=="1" (
  if "%REXENIOT_OFFLINE%"=="1" (
    echo   ERROR: packages need installing but REXENIOT_OFFLINE=1.
    pause
    exit /b 1
  )
  echo       installing packages ^(~300 MB, one time only^) ...
  "%VPY%" -m pip install --upgrade pip --quiet
  "%VPY%" -m pip install -r "%REQ%" --quiet
  if errorlevel 1 (
    echo   ERROR: package installation failed. Run this to see why:
    echo       "%VPY%" -m pip install -r "%REQ%"
    pause
    exit /b 1
  )
  REM kittenml pulls in PyTorch, which the ONNX speech backend never uses.
  REM Skipping its dependencies keeps the folder near 300 MB, not 2.5 GB.
  echo       installing the speech package ^(ONNX only, no PyTorch^) ...
  "%VPY%" -m pip install --no-deps kittenml==0.9.4 --quiet
  if errorlevel 1 (
    echo   ERROR: could not install the speech package.
    pause
    exit /b 1
  )
  >"%MARK%" echo !HASH!
  echo       packages ready
) else (
  echo       packages unchanged, skipping install
)

REM --------------------------------------------------------------- verify --
echo [4/5] Verifying the application...
"%VPY%" -c "import app.main" 
if errorlevel 1 (
  echo.
  echo   ERROR: the application failed to import.
  pause
  exit /b 1
)

REM ----------------------------------------------------------- voice model -
echo [5/5] Preparing the voice...
echo       the first run downloads the voice model ^(~80 MB, one time only^)
echo       loading it and speaking a test line ^(this can take a minute^) ...
REM A failure is reported but not fatal: the app still starts and the admin
REM panel can repair speech later.
"%VPY%" -m app.main --prepare-tts
if errorlevel 1 echo       NOTE: speech is not ready; the app will still start.

echo Starting Sister RexenIOT...
echo.
echo   ----------------------------------------------------------
echo    Open:        http://localhost:%REXENIOT_PORT%/
echo    Admin panel: http://localhost:%REXENIOT_PORT%/admin
echo.
echo    Everything is stored inside this folder.
echo    Close this window to stop.
echo   ----------------------------------------------------------
echo.

"%VPY%" -m app.main --host 127.0.0.1 --port %REXENIOT_PORT%

echo.
echo  Sister RexenIOT has stopped.
pause
endlocal
