"""기존 .env 파일 경로와 기존 32비트 파이썬 연결. 키를 화면에 출력하지 않음."""
import argparse
import json
from pathlib import Path
from common import config,atomic_json,ROOT
from rest_feed import read_env,find_env

def main():
    cfg=config()
    parser=argparse.ArgumentParser();parser.add_argument('--env-file');args=parser.parse_args()
    current=find_env(cfg)
    print('기존 키움 수급 레이더의 .env 또는 local_credentials.json을 연결합니다. API 키를 새로 발급하지 않습니다.')
    value=args.env_file if args.env_file is not None else input('기존 인증 설정 전체 경로 (자동 발견 경로 유지: 엔터): ').strip().strip('"')
    path=value or current
    if path:
        p=Path(path).expanduser().resolve()
        if p.is_dir():p=p/'.env'
        if not p.is_file():raise SystemExit('설정 파일을 찾지 못했습니다. 파일 경로를 확인하세요.')
        env=read_env(p)
        if not (env.get('KIWOOM_APP_KEY') or env.get('KIWOOM_APPKEY')) or not (env.get('KIWOOM_SECRET_KEY') or env.get('KIWOOM_SECRETKEY')):
            raise SystemExit('기존 설정 파일에 필요한 키 변수명이 없습니다. 값은 출력하지 않습니다.')
        cfg['rest']['env_file']=str(p)
        print('기존 REST 인증 설정 확인 완료. 인증 요청은 실시간 실행 때 합니다.')
    else:
        print('REST 설정을 찾지 못했습니다. 환경변수 또는 config.json의 env_file을 지정하면 됩니다.')
    if args.env_file is None:
        python=input('기존 32비트 python.exe 경로 (py -3-32 유지: 엔터): ').strip().strip('"')
        if python:
            if not Path(python).is_file():raise SystemExit('파이썬 실행 파일을 찾지 못했습니다.')
            cfg['ocx']['python_command']=[python]
        module=input('기존 OpenAPI 설치 폴더 (C:/OpenAPI 유지: 엔터): ').strip().strip('"')
        if module:cfg['ocx']['openapi_dir']=module
    atomic_json(ROOT/'config.json',cfg)
    print('연결 설정 저장 완료. 실시간실행.bat을 실행하세요.')

if __name__=='__main__':main()
