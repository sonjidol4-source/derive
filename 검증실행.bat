@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
py -3 -m unittest discover -s tests -v
"%~dp0runtime32\python.exe" -m unittest tests.test_worker_pages -v
pause