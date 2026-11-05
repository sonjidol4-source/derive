"""레이더가 꺼져 있던 날의 장중 분 단위 기록을 LS에서 사후 조회로 채운다(조회 전용, 키·토큰 저장 안 함).

무엇을 언제까지 받을 수 있나(LS 공식 명세, 2026-10-28 확인):
  - 날짜 지정 가능: t8465 선물 1분봉(종가·미결제), t8409 업종 1분봉(KOSPI200 '101'). 봉 시각은 끝 시각(첫 봉 084600).
  - '당일/전일'만: t1602 시간대 투자자(선물 market 4·코스피 현물 market 1, 30초 누적, cts_time으로 이어 받기),
    t1662 시간대 프로그램(차트, 한 번에 하루). 그래서 투자자·프로그램은 LS '금일'·'전일'인 날만 채운다.
기존 실시간 DB는 보존한다. 없는 필드는 별도 복구 DB와 점선 차트에 보충한다.
금일·전일 매핑이 맞는지는 그날 마감 요약의 외국인 선물 최종값(LS t1602 확정)과 마지막 행을 맞춰 본다.
"""
import argparse
import bisect
import datetime as dt
import json
import time
from common import KST,ROOT,business_day,config,last_trading_day,now_kst,number
import close_summary as cs

QUALITY='LS 사후 조회'
QUOTE_TTL,FLOW_TTL=45,150
FUT_QTY={'17':'foreign_fut_qty','18':'inst_fut_qty','08':'individual_fut_qty','01':'financial_fut_qty'}
FUT_AMT={'17':'foreign_fut_amt','18':'inst_fut_amt','08':'individual_fut_amt','01':'financial_fut_amt'}
SPOT_AMT={'17':'foreign_spot_amt','18':'inst_spot_amt','08':'individual_spot_amt','01':'financial_spot_amt'}
PROGRAM={'tot3':'program_amt','cha3':'arb_amt','bcha3':'nonarb_amt'}
QUOTES=('k200','fut_price','fut_prev_close','fut_oi','fut_oi_prev','basis')


def at_of(day,hhmmss):
    s=str(hhmmss or '').strip()
    if len(s)<6 or not s[:6].isdigit():return None
    return dt.datetime.combine(day,dt.time(int(s[:2]),int(s[2:4]),int(s[4:6])),tzinfo=KST)


def bar_series(rows,day,field):
    """1분봉 → [(봉 끝 시각, 값)]. 그날 행만."""
    ymd=day.strftime('%Y%m%d');out=[]
    for r in rows:
        if str(r.get('date'))!=ymd:continue
        t,v=at_of(day,r.get('time')),number(r.get(field))
        if t and v is not None:out.append((t,v))
    return sorted(out)


def investor_series(rows,day,mapping,scale=1):
    """t1602 시간대 행(최신 먼저, 30초 누적) → {key:[(시각,값)]}."""
    out={k:[] for k in mapping.values()}
    for r in rows:
        t=at_of(day,r.get('time'))
        if not t:continue
        for code,key in mapping.items():
            v=number(r.get('sv_'+code))
            if v is not None:out[key].append((t,v*scale))
    return {k:sorted(v) for k,v in out.items() if v}


def program_series(rows,day):
    out={k:[] for k in PROGRAM.values()}
    for r in rows:
        t=at_of(day,r.get('time'))
        if not t:continue
        for f,key in PROGRAM.items():
            v=number(r.get(f))
            if v is not None:out[key].append((t,v*.01))  # 백만원→억원
    return {k:sorted(v) for k,v in out.items() if v}


def quote_series(fut_bars,k200_bars,prev_close,prev_oi):
    """선물 종가·미결제·전일 종가·미결제 전일 대비·KOSPI200·시장 베이시스(선물−지수, LS sbasis와 같은 정의)."""
    price,oi=fut_bars['close'],fut_bars['openyak']
    s={'fut_price':price,'fut_oi':oi,'k200':k200_bars}
    if prev_close:s['fut_prev_close']=[(t,prev_close) for t,_ in price]
    if prev_oi is not None:s['fut_oi_prev']=[(t,v-prev_oi) for t,v in oi]
    k={t:v for t,v in k200_bars}
    s['basis']=[(t,round(v-k[t],2)) for t,v in price if t in k]
    return {key:v for key,v in s.items() if v}


def build_ticks(day,series,sources,segment,code,existing_minutes,start='08:45',end='15:50'):
    """분마다 HH:MM:30 기록 한 개. 각 값은 그 시각 이전 마지막 원자료이고, 신선도 기준(시세 45초·수급 150초)을 넘으면 넣지 않는다."""
    ticks=[];t=dt.datetime.combine(day,dt.time(*map(int,start.split(':'))),tzinfo=KST);stop=dt.datetime.combine(day,dt.time(*map(int,end.split(':'))),tzinfo=KST)
    index={k:([p[0] for p in v],v) for k,v in series.items()}
    while t<=stop:
        at=t+dt.timedelta(seconds=30)
        if at.isoformat()[:16] not in existing_minutes:
            values={}
            for key,(times,points) in index.items():
                i=bisect.bisect_right(times,at)-1
                if i<0:continue
                when,v=points[i];ttl=QUOTE_TTL if key in QUOTES else FLOW_TTL
                if (at-when).total_seconds()<=ttl:
                    values[key]={'value':v,'at':when.isoformat(),'source':sources[key],'quality':QUALITY,
                                 'scope':'market' if key in QUOTES else 'cumulative_day'}
            if values:ticks.append((at.isoformat(),{'values':values,'fut_code':code,'segment':segment}))
        t+=dt.timedelta(minutes=1)
    return ticks


class Ls:
    def __init__(self,cfg):
        import requests
        from ls_probe import BASE,credentials,token
        self.requests,self.base=requests,BASE;self.access=token(*credentials(cfg));self.calls=0

    def call(self,path,tr,block,cont='N',key=''):
        for attempt in range(3):  # 응답 지연(10/28 t1662 15초 초과)은 잠시 뒤 다시 묻는다
            try:
                r=self.requests.post(self.base+path,timeout=30,json={tr+'InBlock':block},
                    headers={'content-type':'application/json; charset=utf-8','authorization':'Bearer '+self.access,'tr_cd':tr,'tr_cont':cont,'tr_cont_key':key})
                break
            except self.requests.RequestException:
                if attempt==2:raise
                time.sleep(5)
        body=r.json();self.calls+=1;time.sleep(1.1)
        if str(body.get('rsp_cd',''))!='00000':raise ValueError('LS %s 응답 오류 %s %s'%(tr,body.get('rsp_cd'),body.get('rsp_msg')))
        return body,r.headers.get('tr_cont'),r.headers.get('tr_cont_key') or ''

    def t1602(self,market,upcode,gubun1,gubun2):
        block={'market':market,'upcode':upcode,'gubun1':gubun1,'gubun2':gubun2,'cts_time':'','cts_idx':0,'cnt':500,'gubun3':'','exchgubun':'K'}
        rows=[];cont,key='N',''
        for _ in range(8):
            body,tc,tk=self.call('/stock/investor','t1602',block,cont,key)
            page=body.get('t1602OutBlock1') or [];rows+=page
            if tc!='Y' or not page:break
            block={**block,'cts_time':(body.get('t1602OutBlock') or {}).get('cts_time','')};cont,key='Y',tk
        return rows

    def bars(self,tr,path,code,sdate,edate):
        body,_,_=self.call(path,tr,{'shcode':code,'ncnt':1,'qrycnt':500,'nday':'0','sdate':sdate,'stime':'','edate':edate,
                                    'etime':'','cts_date':'','cts_time':'','comp_yn':'N'})
        return body.get(tr+'OutBlock1') or []

    def near_future(self):
        from ls_deriv import near_future
        body,_,_=self.call('/futureoption/market-data','t8467',{'gubun':''});return near_future(body)


def previous_day(day,cfg):
    return cs.previous_business_day(day,cfg)


def collect(day,cfg,ls=None,directory=None):
    """Fetch actual historical observations; never writes the live database."""
    directory=directory or ROOT/'data'
    today=last_trading_day(now_kst(),cfg);prev=previous_day(today,cfg)
    gubun2={today:'0',prev:'1'}.get(day)
    ls=ls or Ls(cfg);code=ls.near_future();ymd=day.strftime('%Y%m%d')
    failures={}
    def fetch(name,callback,default):
        try:return callback()
        except Exception as error:
            if type(error).__name__=='FeedStopped':raise
            failures[name]=str(error)[:180];return default
    pday=previous_day(day,cfg);pymd=pday.strftime('%Y%m%d')
    # 시세: 그날과 직전 거래일 선물 1분봉(직전 마지막 봉 = 전일 종가·미결제), 그날 KOSPI200 1분봉
    prev_rows=fetch('전일 선물 분봉',lambda:ls.bars('t8465','/futureoption/chart',code,pymd,pymd),[])
    fut_rows=fetch('당일 선물 분봉',lambda:ls.bars('t8465','/futureoption/chart',code,ymd,ymd),[])
    k_rows=fetch('KOSPI200 분봉',lambda:ls.bars('t8409','/indtp/chart','101',ymd,ymd),[])
    pc=bar_series(prev_rows,pday,'close');po=bar_series(prev_rows,pday,'openyak')
    fut={'close':bar_series(fut_rows,day,'close'),'openyak':bar_series(fut_rows,day,'openyak')}
    series=quote_series(fut,bar_series(k_rows,day,'close'),pc[-1][1] if pc else None,po[-1][1] if po else None)
    sources={k:'LS t8465 선물 1분봉 · 사후 조회' for k in series};sources.update(k200='LS t8409 KOSPI200 1분봉 · 사후 조회',basis='계산: 선물−KOSPI200 1분봉 · 사후 조회')
    report={'day':day.isoformat(),'code':code,'mode':{'0':'LS 금일','1':'LS 전일'}.get(gubun2,'시세만(투자자·프로그램은 당일/전일만 조회 가능)'),
            'bars':{'futures':len(fut['close']),'k200':len(series.get('k200',[])),'prev_close':pc[-1][1] if pc else None,'prev_oi':po[-1][1] if po else None},'checks':[]}
    if gubun2:
        fq=fetch('선물 투자자 수량',lambda:ls.t1602('4','900','1',gubun2),[])
        fa=fetch('선물 투자자 금액',lambda:ls.t1602('4','900','2',gubun2),[])
        sa=fetch('현물 KRX 투자자',lambda:ls.t1602('1','001','2',gubun2),[])
        body,_,_=fetch('프로그램',lambda:ls.call('/stock/program','t1662',{'gubun':'0','gubun1':'0','gubun3':gubun2,'exchgubun':'K'}),({},None,''))
        prog=body.get('t1662OutBlock') or []
        # 날짜 대조 1: 마감 요약에 저장된 외국인 선물 확정값(LS t1602)과 마지막 행
        rec=cs.load(directory).get(day.isoformat())
        last=fq[0].get('sv_17') if fq else None
        if rec and day!=today:
            ok=last==rec['qty'].get('17');report['checks'].append('선물 외국인 마지막 %s vs 마감 요약 %s → %s'%(last,rec['qty'].get('17'),'일치' if ok else '불일치'))
            if not ok:raise ValueError('LS %s 시계열이 %s 마감 요약과 맞지 않아 중단합니다'%(report['mode'],day))
        # 날짜 대조 2: 프로그램 차트의 KOSPI200과 그날 지수 1분봉 마지막 값
        kl=series.get('k200',[])[-1:] ;pk=number(prog[0].get('k200jisu')) if prog else None
        if kl and pk is not None and day!=today:
            ok=abs(kl[0][1]-pk)<0.02;report['checks'].append('프로그램 차트 KOSPI200 %s vs 지수 1분봉 마지막 %s → %s'%(pk,kl[0][1],'일치' if ok else '불일치'))
            if not ok:prog=[]
        series.update(investor_series(fq,day,FUT_QTY));series.update(investor_series(fa,day,FUT_AMT));series.update(investor_series(sa,day,SPOT_AMT))
        series.update(program_series(prog,day))
        for k in FUT_QTY.values():sources[k]='LS t1602 선물 투자자 · 사후 조회'
        for k in FUT_AMT.values():sources[k]='LS t1602 선물 투자자 금액 · 사후 조회'
        for k in SPOT_AMT.values():sources[k]='LS t1602 코스피 투자자 · 사후 조회'
        for k in PROGRAM.values():sources[k]='LS t1662 프로그램 · 사후 조회'
        report['rows']={'fut_investor':len(fq),'spot_investor':len(sa),'program':len(prog)}
    if last_trading_day(now_kst(),cfg)!=today:raise ValueError('조회 중 거래일이 바뀌어 과거 수급의 날짜를 확정할 수 없습니다. 다시 조회하세요.')
    report.update(keys=sorted(series),ls_calls=ls.calls,failures=failures)
    return {'day':day,'code':code,'series':series,'sources':sources,'report':report}


def run(day,cfg,dry=False,ls=None,start='08:45',end='15:50'):
    from gap_recovery import recover
    return recover(day,cfg,ROOT/'data',[(start,end)],ls=ls,dry=dry)


def main():
    p=argparse.ArgumentParser();p.add_argument('days',nargs='+',help='YYYY-MM-DD')
    p.add_argument('--dry-run',action='store_true',help='실제 과거 자료를 조회하되 저장하지 않음')
    p.add_argument('--start',default='08:45',help='복구 시작 HH:MM')
    p.add_argument('--end',default='15:50',help='복구 종료 HH:MM')
    args=p.parse_args();cfg=config()
    for d in args.days:
        day=dt.date.fromisoformat(d)
        if not business_day(day,cfg):print(d,'휴장일이라 건너뜀');continue
        print(json.dumps(run(day,cfg,args.dry_run,start=args.start,end=args.end),ensure_ascii=False,indent=1))


if __name__=='__main__':main()
