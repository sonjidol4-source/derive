@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (python kis_night.py setup) else (py -3 kis_night.py setup)
pause
