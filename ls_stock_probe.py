"""LS OPEN API로 키움 REST가 주던 현물 데이터를 받을 수 있는지 1회 실조회하고 같은 날 키움 값과 나란히 저장한다.

조회만 한다(주문·계좌 없음). 키와 토큰은 파일·화면·로그에 쓰지 않는다. 각 TR 1회, LS는 TR 사이 1.1초 간격.
대응(LS 공식 포털 명세, 2026-10-22 확인 · docs/ls_stock_contracts.md):
  현물 투자자   키움 ka10051(업종별 순매수, 001 종합)   ↔ LS t1602 market=1 upcode=001 gubun1=2(금액) gubun2=0(금일)
  프로그램      키움 ka90005(시간대별 프로그램)         ↔ LS t1632 gubun=0(거래소) gubun1=0(금액) gubun2=1 gubun3=0
  등락 종목     키움 ka20001(코스피 001 상승·하락 수)   ↔ LS t1511 upcode=001 highjo·upjo·unchgjo·lowjo·downjo
  관심종목 수급 키움 ka10063(장중 투자자 잠정, 억원)    ↔ LS t1717 gubun=0(일간) 외인계·기관 순매수량×단가
거래소는 양쪽 모두 KRX(키움 stex_tp=1, LS exchgubun=K)로 맞춘다.
"""
import argparse
import json
import time
import requests
from common import ROOT,atomic_json,config,now_kst,number
from ls_probe import BASE,credentials,token
from rest_feed import Client,read_env,find_env,spot_packet,program_packet,index_packet

INVESTORS=(('17','foreign_spot_amt','외국인'),('18','inst_spot_amt','기관계'),('08','individual_spot_amt','개인'),
           ('01','financial_spot_amt','금융투자(LS 증권)'))


def ls_call(access,path,tr,block):
    r=requests.post(BASE+path,timeout=15,json={tr+'InBlock':block},
        headers={'content-type':'application/json; charset=utf-8','authorization':'Bearer '+access,'tr_cd':tr,'tr_cont':'N','tr_cont_key':''})
    try:body=r.json()
    except ValueError:body={'text':r.text[:500]}
    time.sleep(1.1)
    return {'http':r.status_code,'request':block,'response':body}


def kiwoom_call(client,api,body):
    try:return {'request':body,'response':client.query(api,body)}
    except Exception as e:  # 인증·네트워크·응답 오류도 결과 파일에 남긴다
        return {'request':body,'error':str(e)[:300]}


def probe(day,watch):
    key,secret=credentials(config());access=token(key,secret)
    ymd=day.replace('-','')
    ls={
        't1602':ls_call(access,'/stock/investor','t1602',{'market':'1','upcode':'001','gubun1':'2','gubun2':'0','cts_time':'','cts_idx':0,'cnt':5,'gubun3':'','exchgubun':'K'}),
        # gubun2=1은 '직전 대비 증감'이라 장 마감 뒤엔 0만 온다(10/22 확인). 0이 당일 누적.
        't1632':ls_call(access,'/stock/program','t1632',{'gubun':'0','gubun1':'0','gubun2':'0','gubun3':'0','date':' ','time':' ','exchgubun':'K'}),
        't1511':ls_call(access,'/indtp/market-data','t1511',{'upcode':'001'}),
    }
    for s in watch:
        ls['t1717_'+s['code']]=ls_call(access,'/stock/frgr-itt','t1717',{'shcode':s['code'],'gubun':'0','fromdt':' ','todt':ymd,'exchgubun':'K'})
    client=Client(read_env(find_env(config())))
    kw={
        'ka10051':kiwoom_call(client,'ka10051',{'mrkt_tp':'0','amt_qty_tp':'0','base_dt':ymd,'stex_tp':'1'}),
        'ka90005':kiwoom_call(client,'ka90005',{'date':ymd,'amt_qty_tp':'1','mrkt_tp':'P00101','min_tic_tp':'1','stex_tp':'1'}),
        'ka20001':kiwoom_call(client,'ka20001',{'mrkt_tp':'0','inds_cd':'001'}),
        'ka10063_foreign':kiwoom_call(client,'ka10063',{'mrkt_tp':'001','amt_qty_tp':'1','invsr':'6','frgn_all':'0','smtm_netprps_tp':'0','stex_tp':'1'}),
        'ka10063_inst':kiwoom_call(client,'ka10063',{'mrkt_tp':'001','amt_qty_tp':'1','invsr':'7','frgn_all':'0','smtm_netprps_tp':'0','stex_tp':'1'}),
    }
    return ls,kw


def compare(ls,kw,watch,at):
    out=[];lines=[]
    body=lambda r:(r or {}).get('response') or {}
    def row(name,lsv,kwv,unit,note=''):
        out.append({'item':name,'ls':lsv,'kiwoom':kwv,'unit':unit,'note':note})
        diff='' if lsv is None or kwv is None else ' · 차이 %+.1f'%(lsv-kwv)
        lines.append('%-22s LS %s / 키움 %s %s%s %s'%(name,'—' if lsv is None else format(lsv,',.1f'),'—' if kwv is None else format(kwv,',.1f'),unit,diff,note))
    for tr,r in ls.items():
        b=body(r);lines.append('LS %-12s HTTP %s · %s %s'%(tr,r['http'],b.get('rsp_cd',''),b.get('rsp_msg','')))
    for api,r in kw.items():
        lines.append('키움 %-15s %s'%(api,'오류 '+r['error'] if 'error' in r else '응답'))
    lines.append('')
    # 1) 현물 투자자: LS 요약(OutBlock svolume_*)과 최신 시간대 행(sv_*). 단위는 응답 값으로 판단한다.
    head=body(ls['t1602']).get('t1602OutBlock') or {};last=(body(ls['t1602']).get('t1602OutBlock1') or [{}])[0]
    try:kv=spot_packet(body(kw['ka10051']),at)['values']
    except (ValueError,KeyError,TypeError):kv={}
    lines.append('[현물 투자자 · 코스피 · LS 시간대 최신 %s]'%last.get('time','—'))
    for code,k,name in INVESTORS:
        row('현물 '+name,number(head.get('svolume_'+code)),kv.get(k),'(LS 원값 / 키움 억원)','· LS 시간대 행 %s'%last.get('sv_'+code,'—'))
    # 2) 프로그램: 최신 시간대 행. 양쪽 모두 백만원 → 억원(10/22 원값 −329,420으로 일치 확인).
    rows=body(ls['t1632']).get('t1632OutBlock1') or [];p=rows[0] if rows else {}
    try:kp=program_packet(body(kw['ka90005']),at)
    except (ValueError,KeyError,TypeError):kp={'values':{},'asof':''}
    lines.append('');lines.append('[프로그램 · LS 최신 %s · 키움 최신 %s]'%(p.get('time','—'),str(kp.get('asof',''))[11:19]))
    for f,k,name in (('tot3','program_amt','전체 순매수'),('cha3','arb_amt','차익 순매수'),('bcha3','nonarb_amt','비차익 순매수')):
        row('프로그램 '+name,number(p.get(f),.01),kp['values'].get(k),'억원')
    # 3) 등락 종목: 같은 식으로 상승 비율(상승+상한)/전체.
    u=body(ls['t1511']).get('t1511OutBlock') or {}
    counts=[number(u.get(f)) for f in ('highjo','upjo','unchgjo','lowjo','downjo')]
    lsb=(counts[0]+counts[1])/sum(counts)*100 if all(c is not None for c in counts) and sum(counts) else None
    try:kb=index_packet(body(kw['ka20001']),at)['values'].get('breadth')
    except (ValueError,KeyError,TypeError):kb=None
    kr=body(kw['ka20001'])
    lines.append('');lines.append('[등락 종목 · 코스피] LS 상승 %s·상한 %s·보합 %s·하락 %s·하한 %s / 키움 상승 %s·상한 %s·보합 %s·하락 %s·하한 %s'%(
        *[u.get(f,'—') for f in ('highjo','upjo','unchgjo','lowjo','downjo')],*[kr.get(f,'—') for f in ('rising','upl','stdns','fall','lst')]))
    row('상승 종목 비율',lsb,kb,'%')
    # 4) 관심종목: LS 일간 외인계·기관 순매수량×단가 ≈ 금액(억원). 키움 ka10063 목록에 없으면 빈값.
    kwmap={}
    for inv in ('foreign','inst'):
        rs=body(kw['ka10063_'+inv]).get('opmr_invsr_trde')
        kwmap[inv]={str(r.get('stk_cd','')).split('_')[0]:number(r.get('netprps_amt'),.01) for r in rs} if isinstance(rs,list) else {}
    lines.append('')
    for s in watch:
        d=(body(ls['t1717_'+s['code']]).get('t1717OutBlock') or [{}])[0]
        est=lambda v,p:None if number(v) is None or number(p) is None else number(v)*number(p)/1e8
        lines.append('[관심종목 %s · LS 일자 %s]'%(s['name'],d.get('date','—')))
        row(s['name']+' 외국인',est(d.get('tjj0016_vol'),d.get('tjj0016_dan')),kwmap['foreign'].get(s['code']),'억원','(LS 외인계 %s주×단가 %s)'%(d.get('tjj0016_vol','—'),d.get('tjj0016_dan','—')))
        row(s['name']+' 기관',est(d.get('tjj0018_vol'),d.get('tjj0018_dan')),kwmap['inst'].get(s['code']),'억원','(LS 기관 %s주×단가 %s)'%(d.get('tjj0018_vol','—'),d.get('tjj0018_dan','—')))
    return out,'\n'.join(lines)


def main():
    p=argparse.ArgumentParser();p.add_argument('--day',help='YYYY-MM-DD, 기본 오늘');args=p.parse_args()
    at=now_kst();day=args.day or at.date().isoformat();watch=config()['rest']['watchlist']
    ls,kw=probe(day,watch)
    rows,text=compare(ls,kw,watch,at)
    head='LS ↔ 키움 현물 데이터 1회 비교 · 기준일 %s · 조회 %s (장외 조회값이며 장중 잠정값 비교는 아님)\n'%(day,at.isoformat()[:19])
    atomic_json(ROOT/'data'/'ls_stock_probe.json',{'asked_at':at.isoformat(),'day':day,'ls':ls,'kiwoom':kw,'comparison':rows,
        'note':'조회 1회. 키·토큰은 저장하지 않음.'})
    (ROOT/'data'/'LS_현물_점검.txt').write_text(head+text,encoding='utf-8-sig')
    print(head+text)


if __name__=='__main__':main()
