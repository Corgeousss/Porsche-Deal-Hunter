@echo off
REM ============================================================
REM  What Windows Task Scheduler runs on each tick.
REM  One refresh cycle: re-read approved dealers, update prices,
REM  recompute alerts. Output is appended to data\refresh.log.
REM ============================================================
setlocal
REM Repo root is the parent of this scheduler\ folder.
cd /d "%~dp0.."
set "PORSCHE_DB=data\real.db"

echo. >> "data\refresh.log"
echo ==== scheduled refresh %DATE% %TIME% ==== >> "data\refresh.log"
python -m porschehunter refresh run --trigger scheduled --destination OH >> "data\refresh.log" 2>&1
echo ---- exit code %ERRORLEVEL% ---- >> "data\refresh.log"
endlocal
