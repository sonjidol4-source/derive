@echo off
cd /d "%~dp0"
rem 저장본 미리보기(수집 없음). 수집기가 8767을 쓰는 중이면 먼저 종료하세요.
py -3 -B app.py --preview
pause
