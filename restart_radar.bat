@echo off
cd /d "%~dp0"
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":8767 .*LISTENING"') do taskkill /pid %%p /f
timeout /t 2 /nobreak >nul
start "deriv radar" /min /d "%~dp0" cmd /c "set PYTHONIOENCODING=utf-8 & py -3 app.py & pause"
exit /b 0
