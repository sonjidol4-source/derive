"""Historical archive only. Never injects recovered data into Engine or its signals."""
import datetime as dt
import json
import csv
import io
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from common import KST, stamp, now_kst, number, business_day

FILE='gap_recovery.sqlite3'
PRIMARY=('k200','fut_price','fut_oi','foreign_fut_qty','nonarb_amt')
LABELS={'k200':'KOSPI200','fut_price':'선물 가격','fut_oi':'선물 미결제',
        'foreign_fut_qty':'외국인 선물','nonarb_amt':'비차익 프로그램','basis':'시장 베이시스',
        'foreign_spot_krx_amt':'외국인 현물 · LS KRX'}


def read_live(directory,day):
    path=Path(directory)/'radar.sqlite3'
    if not path.exists():return []
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        return [{'at':a,**json.loads(p)} for a,p in db.execute('SELECT at,payload FROM ticks WHERE day=? ORDER BY at',(str(day),))]


def normalize_ranges(day,ranges):
    if not isinstance(ranges,(list,tuple)) or not 0<len(ranges)<=50:raise ValueError('복구 구간은 최대50개입니다.')
    result=[]
    for pair in ranges:
        if not isinstance(pair,(list,tuple)) or len(pair)!=2 or not all(isinstance(v,str) for v in pair):raise ValueError('시작·종료 HH:MM을 확인하세요.')
        start,end=pair
        a=dt.datetime.combine(day,dt.time.fromisoformat(start),tzinfo=KST)
        b=dt.datetime.combine(day,dt.time.fromisoformat(end),tzinfo=KST)
        if a>b or a.strftime('%H:%M')<'08:45' or b.strftime('%H:%M')>'15:50':
            raise ValueError('복구 구간은 08:45~15:50 안의 시작·종료 시각이어야 합니다.')
        result.append((a.strftime('%H:%M'),b.strftime('%H:%M')))
    if not result or len(result)>50:raise ValueError('복구 구간을 확인하세요.')
    return result


def targets(directory,day,at):
    """Missing minutes and missing primary fields, excluding the last two minutes."""
    rows=read_live(directory,day);by_min={h['at'][:16]:h for h in rows}
    covered=set();archive=Path(directory)/FILE
    if archive.exists():
        with closing(sqlite3.connect(archive.resolve().as_uri()+'?mode=ro',uri=True)) as db:
            covered={(m,k) for m,k in db.execute('SELECT minute,field FROM cells WHERE day=?',(str(day),))}
    t=dt.datetime.combine(day,dt.time(8,45),tzinfo=KST)
    end=min(stamp(at)-dt.timedelta(minutes=2),dt.datetime.combine(day,dt.time(15,50),tzinfo=KST))
    minutes=[]
    while t<=end:
        row=by_min.get(t.isoformat()[:16]);values=(row or {}).get('values',{})
        expected=PRIMARY if t.hour>=9 else tuple(k for k in PRIMARY if k!='k200')
        minute=t.isoformat()[:16]
        if any(not (row and usable(values,k,row['at'])) and (minute,k) not in covered for k in expected):minutes.append(t)
        t+=dt.timedelta(minutes=1)
    groups=[]
    for t in minutes:
        if groups and t-groups[-1][1]==dt.timedelta(minutes=1):groups[-1]=(groups[-1][0],t)
        else:groups.append((t,t))
    return [(a.strftime('%H:%M'),b.strftime('%H:%M')) for a,b in groups][:50]


def usable(values,key,at):
    row=values.get(key) or {}
    if number(row.get('value')) is None or not row.get('at'):return False
    ttl=45 if key in ('k200','fut_price','fut_oi','basis','fut_prev_close','fut_oi_prev') else 150
    return 0 <= (stamp(at)-stamp(row['at'])).total_seconds() <= ttl


def read_view(directory,day):
    path=Path(directory)/FILE
    result={'status':'대기','day':str(day),'history':[],'counts':{},'jobs':[]}
    if not path.exists():return result
    try:
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
            cells=list(db.execute('SELECT minute,field,payload FROM cells WHERE day=? ORDER BY minute,field',(str(day),)))
            result['jobs']=[json.loads(p) for p, in db.execute('SELECT payload FROM jobs WHERE day=? ORDER BY id DESC LIMIT 8',(str(day),))]
    except (sqlite3.Error,ValueError) as error:
        return {**result,'status':'복구 자료 읽기 오류','note':str(error)[:160]+' · 실시간 수집은 계속됩니다.'}
    live={h['at'][:16]:h for h in read_live(directory,day)};history={}
    for minute,key,payload in cells:
        row=json.loads(payload);original=live.get(minute)
        if original and usable(original['values'],key,original['at']):continue
        entry=history.setdefault(minute,{'at':minute+':30+09:00','values':{},'backfilled':True})
        entry['values'][key]=row['value']
        result['counts'][key]=result['counts'].get(key,0)+1
    result['history']=list(history.values())
    result['status']='부분 복구' if result['jobs'] and (result['jobs'][0].get('missing_primary') or result['jobs'][0].get('failures')) else '복구 자료 있음' if cells else '조회된 복구 자료 없음'
    return result


def export_csv(directory,day):
    stream=io.StringIO();columns=('minute','field','value','source_at','retrieved_at','source','scope','code','backfilled')
    writer=csv.DictWriter(stream,fieldnames=columns);writer.writeheader()
    path=Path(directory)/FILE
    if path.exists():
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
            for minute,key,payload in db.execute('SELECT minute,field,payload FROM cells WHERE day=? ORDER BY minute,field',(str(day),)):
                row=json.loads(payload)
                writer.writerow({'minute':minute,'field':key,'value':row['value'],'source_at':row['at'],
                    **{k:row.get(k,'') for k in columns[4:]}})
    return '\ufeff'+stream.getvalue()


def recover(day,cfg,directory,ranges,ls=None,dry=False,pack=None):
    from ls_backfill import collect, build_ticks
    directory=Path(directory);ranges=normalize_ranges(day,ranges)
    pack=pack or collect(day,cfg,ls=ls,directory=directory)
    if str(pack['day'])!=str(day):raise ValueError('복구 조회일이 요청일과 다릅니다.')
    retrieved=now_kst().isoformat();live={h['at'][:16]:h for h in read_live(directory,day)}
    proposed={};codes={h.get('fut_code') for h in live.values() if h.get('fut_code')}
    for start,end in ranges:
        for at,payload in build_ticks(day,pack['series'],pack['sources'],'recovered',pack['code'],set(),start=start,end=end):
            if stamp(at)>stamp(retrieved):continue
            minute=at[:16];original=live.get(minute)
            for key,row in payload['values'].items():
                # LS cash is KRX; the live cash feed is KRX+NXT. Keep a separate field.
                archive_key=key.replace('_spot_amt','_spot_krx_amt') if key.endswith('_spot_amt') else key
                if original and usable(original['values'],archive_key,original['at']):continue
                if ('fut_' in key or key=='basis') and codes and codes!={pack['code']}:continue
                proposed[(minute,archive_key)]={**row,'backfilled':True,'provenance':'recovered',
                    'retrieved_at':retrieved,'code':pack['code'],'scope':'KRX' if '_spot_krx_' in archive_key else row.get('scope','market')}
    report={**pack['report'],'ranges':ranges,'retrieved_at':retrieved,'proposed_fields':len(proposed),
            'added_fields':0,'added_minutes':0,'counts':{},'unsupported':['이론 베이시스','시장−이론 베이시스','KRX+NXT 현물 과거 통합 수급'],
            'note':'사후 조회 자료는 별도 보관하며 실시간 판정·당시 신호에 소급 적용하지 않습니다.'}
    for _,key in proposed:report['counts'][key]=report['counts'].get(key,0)+1
    expected=set()
    for start,end in ranges:
        a=dt.datetime.combine(day,dt.time.fromisoformat(start),tzinfo=KST)
        b=dt.datetime.combine(day,dt.time.fromisoformat(end),tzinfo=KST)
        while a<=b:
            if a+dt.timedelta(seconds=30)>stamp(retrieved):
                a+=dt.timedelta(minutes=1);continue
            minute=a.isoformat()[:16]
            for key in PRIMARY:
                if key=='k200' and a.hour<9:continue
                if not (minute in live and usable(live[minute]['values'],key,live[minute]['at'])):
                    expected.add((minute,key))
            a+=dt.timedelta(minutes=1)
    if dry:
        report['missing_primary']=len(expected-set(proposed));return report
    directory.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(directory/FILE)) as db:
        db.executescript('CREATE TABLE IF NOT EXISTS cells(day TEXT,minute TEXT,field TEXT,payload TEXT,PRIMARY KEY(day,minute,field)); CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY,day TEXT,payload TEXT); CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);')
        backup_key='backup:'+str(day)
        if not db.execute('SELECT 1 FROM meta WHERE key=?',(backup_key,)).fetchone():
            original=directory/'radar.sqlite3'
            if original.exists():
                backups=directory/'backups';backups.mkdir(exist_ok=True)
                destination=backups/('before-gap-recovery-'+now_kst().strftime('%Y%m%d-%H%M%S')+'.sqlite3')
                with closing(sqlite3.connect(original.resolve().as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(destination)) as dst:src.backup(dst)
                db.execute('INSERT INTO meta VALUES(?,?)',(backup_key,str(destination)))
                report['backup']=str(destination)
        added_minutes=set()
        for (minute,key),row in proposed.items():
            cursor=db.execute('INSERT OR IGNORE INTO cells VALUES(?,?,?,?)',(str(day),minute,key,json.dumps(row,ensure_ascii=False)))
            if cursor.rowcount:report['added_fields']+=1;added_minutes.add(minute)
        report['added_minutes']=len(added_minutes)
        existing={(minute,key) for minute,key in db.execute('SELECT minute,field FROM cells WHERE day=?',(str(day),))}
        report['missing_primary']=len(expected-existing)
        db.execute('INSERT INTO jobs(day,payload) VALUES(?,?)',(str(day),json.dumps(report,ensure_ascii=False)))
        db.commit()
    return report


def kis_view(directory,day,result=None):
    """화면용: 한투 차트 공백을 LS t1617로 메운 점들. 실패해도 화면은 그대로 둔다."""
    try:
        import kis_gap,deriv_today as D
        d=dt.date.fromisoformat(str(day)) if day else now_kst().date()
        view=kis_gap.view(directory,d,D.load_day(directory,str(d)))
        if result:view['last']=result
        return view
    except Exception as error:
        return {'points':[],'error':str(error)[:160]}


class SharedLs:
    """Reuse the live feed's token and request gate; max 48 calls per recovery job."""
    def __init__(self,feed):self.feed=feed;self.calls=0;self.started=time.monotonic();self.started_at=now_kst()
    def call(self,path,tr,block,cont='N',key=''):
        import requests
        from ls_probe import BASE
        for attempt in range(3):
            self.feed.check_stop()
            if self.calls>=48 or time.monotonic()-self.started>180 or (now_kst()-self.started_at).total_seconds()>180 or now_kst().date()!=self.started_at.date():raise ValueError('복구 조회 한도(48회·3분)에 도달했습니다. 절전·거래일 변경 시 다시 조회합니다.')
            access=self.feed.token(requests,*self.feed.keys)
            self.feed.gate.acquire(tr);self.calls+=1
            response=requests.post(BASE+path,timeout=(15,30),json={tr+'InBlock':block},
                headers={'content-type':'application/json; charset=utf-8','authorization':'Bearer '+access,
                         'tr_cd':tr,'tr_cont':cont,'tr_cont_key':key})
            self.feed.check_stop();body=response.json()
            if response.status_code==401:self.feed.invalidate_token(access)
            if str(body.get('rsp_cd'))=='00000':return body,response.headers.get('tr_cont'),response.headers.get('tr_cont_key') or ''
            if str(body.get('rsp_cd'))!='IGW00201' or attempt==2:
                raise ValueError('LS %s 복구 조회 오류 %s %s'%(tr,body.get('rsp_cd'),body.get('rsp_msg','')))
            if self.feed.stop.wait(2*(attempt+1)):self.feed.check_stop()
    def near_future(self):
        from ls_backfill import Ls
        return self.feed.engine.fut_code or Ls.near_future(self)
    def t1602(self,*args):
        from ls_backfill import Ls
        return Ls.t1602(self,*args)
    def bars(self,*args):
        from ls_backfill import Ls
        return Ls.bars(self,*args)


class RecoveryWorker(threading.Thread):
    def __init__(self,cfg,engine,feed,stop):
        super().__init__(daemon=True,name='gap-recovery')
        self.cfg,self.engine,self.feed,self.stop=cfg,engine,feed,stop
        self.pending=None;self.lock=threading.Lock();self.seen={};self.attempts={};self.jobs={};self.next_auto=time.monotonic()+30
        self.next_kis=time.monotonic()+20;self.kis_result=None
        self.state={'status':'대기','note':'절전 복귀·중간 접속의 누락 분을 확인합니다.'}
        self.view=read_view(engine.store.directory,now_kst().date())
        for job in self.view.get('jobs',[]):
            if job.get('ranges'):self.seen[json.dumps((self.view['day'],job['ranges']))]=True
    def request(self,ranges=None):
        day=now_kst().date()
        normalized=normalize_ranges(day,ranges) if ranges else None
        with self.lock:
            if self.pending is not None or self.state['status']=='복구 조회 중':return False
            self.pending=normalized or []
        return True
    def snapshot(self):
        with self.lock:
            state={**self.view,**self.state}
            if state['status']=='대기' and self.view.get('counts'):state['status']=self.view['status']
            kis_result=self.kis_result
        state['kis']=kis_view(self.engine.store.directory,state.get('day'),kis_result)
        return state
    def kis_step(self,now,day):
        """한투 1분 기록 공백을 LS t1617로 메운다(1분마다 확인, 공백이 없으면 조회 안 함). 장 마감 뒤 16:30까지도 확인."""
        if not '08:50'<=now.strftime('%H:%M')<='16:30' or time.monotonic()<self.next_kis:return
        self.next_kis=time.monotonic()+60
        import kis_gap,deriv_today as D
        records=D.load_day(self.engine.store.directory,str(day))
        if not records:return  # 한투를 쓰지 않는 날(키 없음 등)은 메울 대상이 없다
        ls=SharedLs(self.feed)
        result=kis_gap.recover(lambda p,t,b,c:ls.call(p,t,b,c)[:2],self.engine.store.directory,day,records,now)
        with self.lock:self.kis_result=result
    def run(self):
        while not self.stop.is_set():
            try:self._run()
            except Exception as error:
                if self.stop.is_set():return
                with self.lock:self.state={'status':'복구 확인 오류','note':str(error)[:180]+' · 실시간 수집은 계속됩니다.'}
                if self.stop.wait(60):return
    def _run(self):
        while not self.stop.wait(2):
            now=now_kst();day=now.date()
            if self.view.get('day')!=str(day):
                with self.lock:self.view=read_view(self.engine.store.directory,day)
            if not self.feed.keys or not business_day(day,self.cfg):continue
            try:self.kis_step(now,day)
            except Exception as error:
                if self.stop.is_set():return
                with self.lock:self.kis_result={'error':str(error)[:200]}
            with self.lock:
                manual=self.pending is not None;ranges=self.pending;self.pending=None
            if not manual:
                if not '09:05'<=now.strftime('%H:%M')<='15:55' or time.monotonic()<self.next_auto:continue
                self.next_auto=time.monotonic()+60
            ranges=ranges or targets(self.engine.store.directory,day,now)
            if manual and not ranges and self.view.get('jobs'):ranges=self.view['jobs'][0].get('ranges',[])
            if not ranges:continue
            signature=json.dumps((str(day),ranges))
            attempts=self.attempts.get(signature,0)
            if not manual and (self.seen.get(signature) or attempts>=3 or self.jobs.get(str(day),0)>=12):
                if self.jobs.get(str(day),0)>=12:
                    with self.lock:self.state={'status':'자동 조회 한도 도달','note':'오늘 자동 복구12회에 도달했습니다. 필요한 경우 버튼으로 다시 요청할 수 있습니다.'}
                continue
            self.attempts[signature]=attempts+1;self.jobs[str(day)]=self.jobs.get(str(day),0)+1
            with self.lock:self.state={'status':'복구 조회 중','note':'실시간 조회와 호출 간격을 공유합니다. 기존 실시간 자료는 유지됩니다.','requested_ranges':ranges}
            try:
                report=recover(day,self.cfg,self.engine.store.directory,ranges,ls=SharedLs(self.feed))
                self.seen[signature]=not bool(report.get('failures'))
                view=read_view(self.engine.store.directory,day)
                with self.lock:
                    self.view=view;self.state={'status':view['status'],'note':report['note'],'last_report':report}
            except Exception as error:
                if self.stop.is_set():return
                with self.lock:self.state={'status':'복구 조회 실패','note':str(error)[:200]+' · 같은 구간 자동 시도는 최대3회입니다.','requested_ranges':ranges}
                self.next_auto=time.monotonic()+120
