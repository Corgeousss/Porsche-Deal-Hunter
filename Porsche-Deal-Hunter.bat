@echo off
REM ============================================================
REM  Porsche 911 Deal Hunter - one-click launcher
REM  Double-click this file. No commands to type.
REM ============================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0"

REM Real, production database. Never synthetic data.
set "PORSCHE_DB=data\real.db"

REM The two-letter state where your friend's shop is (drives the transport
REM estimate). Change OH below to your state if needed, e.g. set "DEST=CA".
set "DEST=OH"

REM --- Make sure Python is installed -------------------------------------
where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo   Python was not found on this computer.
  echo   Install it from https://www.python.org/downloads/ and be sure to
  echo   tick "Add python.exe to PATH" on the first screen, then try again.
  echo.
  pause
  exit /b 1
)

REM --- Create the database the first time -------------------------------
if not exist "data\real.db" (
  echo Setting up your database for the first time...
  python -m porschehunter init
)

:menu
cls
echo ============================================================
echo    PORSCHE 911 DEAL HUNTER
echo    Database: %PORSCHE_DB%   Destination state: %DEST%
echo ============================================================
echo.
echo   1.  Open the deal-hunter dashboard  (filters, saved searches,
echo         alerts, reconditioning form -- all in your web browser)
echo   2.  List every car currently in the database
echo   3.  Refresh inventory from all approved dealers  (slow: ~5 min each)
echo   4.  Scan for opportunities that clear the $8,000 target
echo   5.  Show which data sources are working / blocked
echo   6.  Recompute saved-search alerts now
echo   7.  Check my internet + re-verify the connectors
echo   8.  Quit
echo.
set "choice="
set /p choice="Type a number (1-8) and press Enter: "

if "%choice%"=="1" goto dashboard
if "%choice%"=="2" goto listings
if "%choice%"=="3" goto refresh
if "%choice%"=="4" goto scan
if "%choice%"=="5" goto sources
if "%choice%"=="6" goto alerts
if "%choice%"=="7" goto validate
if "%choice%"=="8" exit /b 0
goto menu

:dashboard
echo.
echo Opening http://127.0.0.1:8000 in your browser...
echo The dashboard defaults to 911s under $100,000. Use the left-hand filters
echo to change price, generation, variant, mileage and more; Save search to
echo store a search and turn on alerts; the bell shows notifications.
echo When you are finished, click back in THIS window and press Ctrl+C.
start "" "http://127.0.0.1:8000"
python -m porschehunter serve --destination %DEST%
goto menu

:listings
echo.
python -m porschehunter listings
echo.
pause
goto menu

:refresh
echo.
echo Reading published inventory from every approved dealer in
echo data\allowed_domains.txt. This is slow on purpose (one page every 5
echo seconds, so we stay a polite visitor). Please wait...
echo.
for /f "usebackq eol=# tokens=* delims= " %%D in ("data\allowed_domains.txt") do (
  echo   --- %%D ---
  python -m porschehunter fetch dealer_jsonld --domain %%D --max-pages 60
)
echo.
echo Recomputing saved-search alerts...
python -m porschehunter alerts run --destination %DEST%
echo.
pause
goto menu

:alerts
echo.
python -m porschehunter alerts run --destination %DEST%
echo.
python -m porschehunter alerts list --limit 20
echo.
pause
goto menu

:scan
echo.
python -m porschehunter scan --destination %DEST%
echo.
echo NOTE: cars show as UNVALUED until you have at least 3 verified completed
echo sales recorded for that kind of car. That is by design - the tool will
echo not invent a value.
echo.
pause
goto menu

:sources
echo.
python -m porschehunter sources
echo.
pause
goto menu

:validate
echo.
python -m porschehunter validate --destination %DEST%
echo.
pause
goto menu
