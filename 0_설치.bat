@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
echo [1/2] 기존 일반 Python에 REST 조회 라이브러리 설치
py -3 -m pip install -r requirements.txt
if errorlevel 1 (
  echo REST 설치가 실패했습니다. 기존 Python 환경을 확인하세요.
  pause
  exit /b 1
)
echo [2/2] 기존 32비트 Python에 OCX 라이브러리 설치
py -3-32 -c "import struct; assert struct.calcsize('P') == 4"
if errorlevel 1 (
  echo 기존 32비트 Python을 찾지 못했습니다. 연결설정에서 기존 실행 경로를 지정하세요.
  pause
  exit /b 1
)
py -3-32 -m pip install -r requirements_ocx.txt
if errorlevel 1 (
  echo OCX 설치 실패. Python 3.10 32비트 또는 기존 PyQt5 환경을 확인하세요.
  pause
  exit /b 1
)
echo 설치 완료. 연결설정.bat에서 기존 .env 경로를 지정하세요.
pause
