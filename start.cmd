@echo off
REM ============================================================================
REM  Sister RexenIOT - Windows launcher
REM
REM  Checks prerequisites, prepares the Python environment, then starts the app
REM  and opens the browser. Safe to run repeatedly: existing environments and
REM  downloaded models are reused.
REM ============================================================================

setlocal EnableDelayedExpansion
title Sister RexenIOT - starting

cd /d "%~dp0"

set "VENV=.venv"
set "PY=%VENV%\Scripts\python.exe"
set "REQ=requirements.txt"
set "MARK=%VENV%\.req-hash"
set "PORT=8060"
set "HOST=localhost"

echo.
echo  ==========================================================
echo    S I S T E R   R E X E N I O T
echo    keeper of the house
echo  ==========================================================
echo.

REM ---------------------------------------------------------------- Python ---
echo [1/6] Checking for Python...

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
  pause
  exit /b 1
)

REM Verify the version is at least 3.10; the launcher uses `py -3` otherwise.
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
for /f "delims=" %%v in ('%PYEXE% --version 2^>^&1') do set "PYVER=%%v"
echo       found: !PYVER!

REM ------------------------------------------------------------------ venv ---
echo [2/6] Preparing the virtual environment...

if not exist "%PY%" (
  echo       creating %VENV% ...
  %PYEXE% -m venv "%VENV%"
  if errorlevel 1 (
    echo.
    echo   ERROR: Could not create the virtual environment.
    echo   On Debian-based systems run: sudo apt install python3-venv
    echo.
    pause
    exit /b 1
  )
) else (
  echo       reusing existing %VENV%
)

REM ---------------------------------------------------------- dependencies ---
echo [3/6] Checking dependencies...

REM Hash the requirements file so an unchanged file skips the pip run.
set "HASH="
for /f "skip=1 delims=" %%h in ('certutil -hashfile "%REQ%" SHA256 2^>nul') do (
  if not defined HASH set "HASH=%%h"
)
set "HASH=!HASH: =!"

set "NEED_INSTALL=1"
if exist "%MARK%" (
  set /p OLDHASH=<"%MARK%"
  if "!OLDHASH!"=="!HASH!" set "NEED_INSTALL=0"
)

if "!NEED_INSTALL!"=="1" (
  echo       installing packages ^(this is slow the first time^) ...
  "%PY%" -m pip install --upgrade pip --quiet
  "%PY%" -m pip install -r "%REQ%" --quiet
  if errorlevel 1 (
    echo.
    echo   ERROR: Dependency installation failed.
    echo   Try running this manually to see the full output:
    echo       "%PY%" -m pip install -r "%REQ%"
    echo.
    pause
    exit /b 1
  )
  REM kittenml pulls in torch unless we hold its dependencies back. The ONNX
  REM backend does not need torch, and skipping it saves about 2.5 GB.
  echo       installing the speech package ^(ONNX only, no PyTorch^) ...
  "%PY%" -m pip install --no-deps kittenml==0.9.4 --quiet
  if errorlevel 1 (
    echo.
    echo   ERROR: Could not install the speech package.
    echo   Run: "%PY%" -m pip install --no-deps kittenml==0.9.4
    echo.
    pause
    exit /b 1
  )
  echo !HASH!>"%MARK%"
  echo       dependencies ready
) else (
  echo       dependencies unchanged, skipping install
)

REM ------------------------------------------------------------- gpu note ----
REM The default install is CPU-only on purpose: onnxruntime is small and works
REM everywhere. A GPU only helps if the runtime can reach it, so this detects the
REM card and points at the right requirements file instead of guessing.
REM
REM The torch line is deliberately not a plain "pip install -r requirements-full.txt":
REM on Windows PyPI serves a CPU-only torch at the same version as the Linux CUDA
REM build, so that command reinstalls the identical CPU wheel and the GPU stays
REM unused. The CUDA wheel index has to be named. See the admin panel's hardware
REM tab, which prints this advice for the card it actually finds.
set "GPU_FOUND=0"
where nvidia-smi >nul 2>&1 && (
  nvidia-smi -L >nul 2>&1 && set "GPU_FOUND=1"
)
if "!GPU_FOUND!"=="1" (
  echo       detected NVIDIA GPU:
  for /f "delims=" %%g in ('nvidia-smi --query-gpu^=name,memory.total --format^=csv,noheader 2^>nul') do echo         %%g
  "%PY%" -c "import onnxruntime as o; raise SystemExit(0 if 'CUDAExecutionProvider' in o.get_available_providers() else 1)" >nul 2>&1
  if errorlevel 1 (
    echo.
    echo       NOTE: a GPU is present but the speech runtime cannot use it yet.
    echo             For faster speech, install the GPU build of onnxruntime:
    echo                 "%PY%" -m pip uninstall -y onnxruntime
    echo                 "%PY%" -m pip install -r requirements-gpu.txt
    echo             If that build will not load, DirectML reaches the same GPU
    echo             with no CUDA toolkit:
    echo                 "%PY%" -m pip install -r requirements-directml.txt
    echo             For the 1.7B tier on GPU, torch must come from the CUDA index,
    echo             not PyPI ^(whose Windows wheel is CPU-only^). Run both, in order:
    echo                 "%PY%" -m pip install -r requirements-full.txt
    echo                 "%PY%" -m pip install torch torchaudio --upgrade --index-url https://download.pytorch.org/whl/cu130
    echo.
  )
)

REM --------------------------------------------------------------- config ----
echo [4/6] Checking configuration...

if not exist "data" mkdir "data"
if not exist "data\models" mkdir "data\models"

"%PY%" -c "import app.config as c; c.load(); print('      configuration ready')"
if errorlevel 1 (
  echo.
  echo   ERROR: The configuration could not be loaded.
  echo   Run "%PY%" -c "import app.config" to see the traceback.
  echo.
  pause
  exit /b 1
)

REM ------------------------------------------------- optional: Alexa CLI ----
REM Only needed when the Alexa skill is driven by alexacli. The admin panel
REM reports the same thing, so a miss here is a hint rather than a failure.
"%PY%" -c "import app.config as c; raise SystemExit(0 if c.load().tools.resolved_alexa_provider()=='alexacli' else 1)" >nul 2>&1
if not errorlevel 1 (
  where alexacli >nul 2>&1
  if errorlevel 1 (
    echo.
    echo       NOTE: the Alexa skill is set to the alexacli provider, but the
    echo             'alexacli' command is not on PATH. Install it with:
    echo                 brew install buddyh/tap/alexacli
    echo             or set its full path in the admin panel.
    echo.
  )
)

REM --------------------------------------------------------------- speech ----
echo [5/6] Preparing speech engine...
echo       the first run downloads the voice model ^(~80 MB, one time only^)
echo       loading it and speaking a test line ^(this can take a minute^) ...
REM Loading alone is not "ready": the weights can load while speech is still
REM broken. This loads the model and speaks the active persona's greeting.
REM A failure is reported but not fatal, so the app still starts.
"%PY%" -m app.main --prepare-tts
if errorlevel 1 echo       NOTE: speech is not ready; the app will still start.

REM ------------------------------------------------------------------ run ----
echo [6/6] Starting Sister RexenIOT...

REM Warn if the port is already taken so the failure is obvious.
netstat -ano | findstr /r /c:":%PORT% .*LISTENING" >nul 2>nul
if not errorlevel 1 (
  echo.
  echo   NOTE: port %PORT% is already in use, so RexenIOT may fail to start.
  echo   Close the other process or change the port in the admin panel.
  echo.
)

echo.
echo   ----------------------------------------------------------
echo    Opening http://%HOST%:%PORT%/
echo    Admin panel:  http://%HOST%:%PORT%/admin
echo.
echo    Keep this window open while RexenIOT is running.
echo    Press Ctrl+C to stop.
echo   ----------------------------------------------------------
echo.

"%PY%" -m app.main --host %HOST% --port %PORT%

echo.
echo   Sister RexenIOT has stopped.
pause
endlocal
