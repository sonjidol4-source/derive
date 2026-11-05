@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
set RADAR_TRIES=0
:run
"%~dp0runtime32\python.exe" realtime_investor_probe.py --seconds 180
if not "%errorlevel%"=="77" goto end
set /a RADAR_TRIES+=1
if %RADAR_TRIES% GEQ 3 goto end
"%~dp0runtime32\python.exe" -c "import version_guard as v; raise SystemExit(0 if v.wait_updater() else 1)"
if errorlevel 1 goto end
goto run
:end
pause