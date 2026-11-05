"""LS증권 OPEN API t1602(시간대별투자자매매추이)로 선물 투자자 수급이 나오는지 1회 확인.

조회만 한다(주문·계좌 없음). 키와 토큰은 파일·화면·로그에 쓰지 않는다.
입력: market=4(선물), upcode=900(선물), gubun1=1(수량), gubun2=0 금일 / 1 전일, exchgubun=K(KRX).
출처: 외부 검토가 LS 공식 표에서 확인한 내용. 이 세션에서는 포털 오류로 직접 재확인하지 못했다.
"""
import argparse
import json
from pathlib import Path
import requests
from common import ROOT,atomic_json,config,now_kst

BASE='https://openapi.ls-sec.co.kr:8080'
DEFAULT_CREDENTIALS=r'C:\Users\HP\Documents\Codex\secrets\ls_openapi.json'


def credentials(cfg):
    path=Path((cfg.get('ls') or {}).get('credentials_file') or DEFAULT_CREDENTIALS)
    data=json.loads(path.read_text(encoding='utf-8-sig'))
    if not data.get('app_key') or not data.get('app_secret'):raise ValueError('LS 키 파일에 app_key/app_secret이 없습니다: '+str(path))
    return data['app_key'],data['app_secret']


def token(key,secret):
    r=requests.post(BASE+'/oauth2/token',headers={'content-type':'application/x-www-form-urlencoded'},
                    data={'grant_type':'client_credentials','appkey':key,'appsecretkey':secret,'scope':'oob'},timeout=10)
    body=r.json() if r.headers.get('content-type','').startswith('application/json') else {'text':r.text[:300]}
    if r.status_code!=200 or 'access_token' not in body:
        body.pop('access_token',None)
        raise RuntimeError('토큰 발급 실패 HTTP %s: %s'%(r.status_code,json.dumps(body,ensure_ascii=False)[:300]))
    return body['access_token']


def t1602(access,gubun2='1',cnt=20,cts_time='',cts_idx=0,gubun1='1'):
    block={'market':'4','upcode':'900','gubun1':gubun1,'gubun2':gubun2,'cts_time':cts_time,'cts_idx':cts_idx,'cnt':cnt,'gubun3':'','exchgubun':'K'}
    r=requests.post(BASE+'/stock/investor',timeout=10,
        headers={'content-type':'application/json; charset=utf-8','authorization':'Bearer '+access,'tr_cd':'t1602','tr_cont':'N','tr_cont_key':''},
        json={'t1602InBlock':block})
    try:body=r.json()
    except ValueError:body={'text':r.text[:500]}
    return {'http':r.status_code,'request':block,'response':body,'tr_cont':r.headers.get('tr_cont'),'tr_cont_key':r.headers.get('tr_cont_key')}


def summary(result):
    body=result['response'];lines=['LS t1602 선물 투자자 1회 조회','HTTP %s · %s %s'%(result['http'],body.get('rsp_cd',''),body.get('rsp_msg',''))]
    head=body.get('t1602OutBlock') or {}
    names={'08':'개인','17':'외국인','18':'기관계'}
    for k,n in names.items():
        if 'svolume_'+k in head:lines.append('요약 %s 순매수: %s'%(n,head['svolume_'+k]))
    rows=body.get('t1602OutBlock1') or []
    lines.append('시간대 행: %d'%len(rows))
    for r in rows[:5]:lines.append('  %s 외국인 %s / 기관계 %s / 개인 %s'%(r.get('time'),r.get('sv_17'),r.get('sv_18'),r.get('sv_08')))
    lines.append('단위·집계 범위(근월물/전체 월물/미니 포함)는 같은 날 영웅문 0780과 대조해야 확정됩니다.')
    return '\n'.join(lines)


def main():
    p=argparse.ArgumentParser();p.add_argument('--today',action='store_true',help='금일(gubun2=0). 기본은 전일(1)')
    p.add_argument('--amount',action='store_true',help='금액(gubun1=2). 기본은 수량(1)')
    args=p.parse_args()
    key,secret=credentials(config())
    access=token(key,secret)
    result=t1602(access,'0' if args.today else '1',gubun1='2' if args.amount else '1')
    result.update(asked_at=now_kst().isoformat(),note='조회 1회. 키·토큰은 저장하지 않음.')
    tag=('today' if args.today else 'prev')+('_amount' if args.amount else '_qty')
    atomic_json(ROOT/'data'/('ls_t1602_probe_%s.json'%tag),result)
    text=summary(result)
    (ROOT/'data'/('LS_t1602_점검_%s.txt'%tag)).write_text(text,encoding='utf-8-sig')
    print(text)


if __name__=='__main__':main()
