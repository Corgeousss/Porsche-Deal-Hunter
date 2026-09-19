@echo off
REM ============================================================
REM  Porsche 911 Deal Hunter - one-click dashboard launcher.
REM  Double-click this file. It starts the dashboard server in its
REM  OWN window and opens your browser once the server is ready.
REM
REM  The server runs independently of Claude Code and of this
REM  launcher window: it lives in the window titled
REM  "Porsche Deal Hunter Server". Keep that window open to keep
REM  the dashboard available; close it to stop the dashboard.
REM ============================================================
setlocal
cd /d "%~dp0"
set "PORSCHE_DB=data\real.db"
set "DEST=OH"
set "URL=http://127.0.0.1:8000"

REM --- Python present? --------------------------------------------------
where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo   Python was not found. Install it from https://www.python.org/downloads/
  echo   and tick "Add python.exe to PATH" on the first screen, then try again.
  echo.
  pause
  exit /b 1
)

REM --- First-run database create ---------------------------------------
if not exist "data\real.db" (
  echo Setting up your database for the first time...
  python -m porschehunter init
)

REM --- Already running? Just open the browser. -------------------------
powershell -NoProfile -Command "try{(Invoke-WebRequest -UseBasicParsing %URL%/ -TimeoutSec 2)^|Out-Null; exit 0}catch{exit 1}" >nul 2>nul
if not errorlevel 1 (
  echo Dashboard is already running. Opening %URL% ...
  start "" %URL%
  exit /b 0
)

REM --- Start the server in its own persistent window -------------------
echo Starting the Porsche Deal Hunter dashboard server...
start "Porsche Deal Hunter Server (keep this window open)" /D "%~dp0" cmd /k "set PORSCHE_DB=data\real.db& python -m porschehunter serve --destination %DEST%"

REM --- Wait until it is actually listening, then open the browser ------
echo Waiting for the server to come up...
powershell -NoProfile -Command "$ok=$false; for($i=0;$i -lt 40;$i++){ try{ (Invoke-WebRequest -UseBasicParsing %URL%/ -TimeoutSec 2)^|Out-Null; $ok=$true; break }catch{ Start-Sleep -Milliseconds 750 } }; if($ok){exit 0}else{exit 1}"
if errorlevel 1 (
  echo.
  echo   The server did not respond in time. Look at the window titled
  echo   "Porsche Deal Hunter Server" for the actual error message.
  echo.
  pause
  exit /b 1
)

echo Opening %URL% ...
start "" %URL%
echo.
echo ============================================================
echo  Dashboard is running at %URL%
echo  It lives in the separate "Porsche Deal Hunter Server" window.
echo  Keep that window open to keep the dashboard available.
echo  You can safely close THIS window.
echo ============================================================
timeout /t 8 >nul
exit /b 0
