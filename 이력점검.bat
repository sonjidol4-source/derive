@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (python history_probe.py) else (py -3 history_probe.py)
pause
