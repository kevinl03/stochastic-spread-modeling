@echo off
REM ========================================================================
REM Auto-restarting wrapper for the full collection orchestrator.
REM Runs the stat-arb collector + paper traders + fill-rate trackers under
REM one supervisor (collect_all.py), and relaunches the supervisor itself if
REM it ever crashes. Survives laptop sleep (process resumes on wake).
REM
REM Usage:
REM   run_all.bat                                  -- default 168h (7 days)
REM   run_all.bat --hours 168 --interval 60 --slow-every 10
REM   run_all.bat --skip-fill-rate                 -- collector + paper only
REM
REM To stop cleanly: press Ctrl+C in this window (the supervisor shuts all
REM child processes down gracefully), then answer Y to "Terminate batch job".
REM
REM NOTE ON DATA CONTINUITY: collect_all.py does not resume its embedded
REM stat-arb run. If the supervisor is relaunched after a crash, a NEW
REM data/statarb/<run_id>/ directory is created. No data is lost - it is just
REM split across run dirs (each still partitioned by UTC day). If you need a
REM single uninterrupted stat-arb dataset, run experiments\run_collector.bat
REM instead (that path resumes the same run), and launch paper trading
REM separately.
REM ========================================================================

setlocal enabledelayedexpansion

cd /d "%~dp0\.."

REM Default args if none provided
set "ARGS=%*"
if "%ARGS%"=="" set "ARGS=--hours 168 --interval 60 --slow-every 10"

:LOOP
echo.
echo [%date% %time%] Starting orchestrator...
echo.

python -m experiments.collect_all %ARGS%

set "EXIT_CODE=%ERRORLEVEL%"

REM Exit code 0 = completed normally (duration reached) or graceful Ctrl+C.
if %EXIT_CODE%==0 (
    echo.
    echo [DONE] Orchestrator finished normally.
    goto :END
)

echo.
echo [CRASH] Orchestrator exit code %EXIT_CODE%. Relaunching in 10 seconds...
echo         (Press Ctrl+C now to stop for good.)
timeout /t 10 /nobreak >nul
goto :LOOP

:END
echo.
echo [EXIT] Orchestrator wrapper stopped at %date% %time%.
pause
