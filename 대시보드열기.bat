@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
rem Server already running: just open the dashboard.
netstat -ano | findstr /r /c:":8767 .*LISTENING" >nul
if not errorlevel 1 (
  start "" "http://127.0.0.1:8767/"
  exit /b 0
)
rem Otherwise run the server in this window; app.py opens the browser. Closing this window stops the server.
title deriv radar
where py >nul 2>nul
if errorlevel 1 (python app.py) else (py -3 app.py)
pause
