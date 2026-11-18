@echo off
rem Starts the REK combo macros in this window. Close the window (or press Ctrl+C) to stop.
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 rek_combo.py %*
) else (
    python rek_combo.py %*
)
pause
