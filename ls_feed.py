"""LS증권 OPEN API t1602로 KOSPI200 선물 투자자별 당일 순매수(계약·억원)를 장중 자동 수집.

2026-10-17 대조: 10/16 금액(억원) 개인 -2,414 / 외국인 -9,540 / 기관계 +11,456은 영웅문 0780과 같았다.
수량 -1,099 / -4,200 / +5,077계약은 증권사 앱 화면과 같았다.
LS 투자자 코드: 08 개인, 17 외국인, 18 기관계, 01 증권(금융투자로 표시), 07 기타.
조회만 하며 키·토큰은 저장·출력하지 않는다.
"""
import datetime as dt
import json
import threading
import time
import close_summary as cs
from common import KST,now_kst,in_session,business_day,last_trading_day
from ls_probe import BASE,credentials

QTY_KEYS={'17':'foreign_fut_qty','18':'inst_fut_qty','08':'individual_fut_qty','01':'financial_fut_qty'}
AMT_KEYS={'17':'foreign_fut_amt','18':'inst_fut_amt','08':'individual_fut_amt','01':'financial_fut_amt'}


def row_time(value,today):
    """'HHMMSScc' → 오늘 한국시간. 형식이 다르면 None."""
    s=str(value or '').strip()
    if len(s)<6 or not s[:6].isdigit():return None
    h,m,sec=int(s[:2]),int(s[2:4]),int(s[4:6])
    if h>23 or m>59 or sec>59:return None
    return dt.datetime.combine(today,dt.time(h,m,sec),tzinfo=KST)


def ls_packet(body,keys,received):
    """t1602 응답의 최신 시간대 행을 엔진 수급 패킷으로. 결측은 넣지 않는다(0으로 바꾸지 않음)."""
    if str(body.get('rsp_cd',''))!='00000':raise ValueError('LS 응답 오류 %s %s'%(body.get('rsp_cd'),body.get('rsp_msg')))
    rows=body.get('t1602OutBlock1') or []
    if not rows:raise ValueError('LS t1602 시간대 행이 없습니다')
    latest=rows[0];at=row_time(latest.get('time'),received.date())
    if at is None:raise ValueError('LS 시간 형식 확인 필요: %r'%latest.get('time'))
    if at>received+dt.timedelta(seconds=60):raise ValueError('LS 데이터 시각이 현재보다 늦습니다(%s)'%at.time())
    values={}
    for code,key in keys.items():
        v=latest.get('sv_'+code)
        if isinstance(v,(int,float)) and not isinstance(v,bool):values[key]=float(v)
    if not values:raise ValueError('LS 응답에 투자자 수량이 없습니다')
    return {'asof':at.isoformat(),'source':'LS t1602','values':values,'quality':'장중 잠정','scope':'cumulative_day'}


PROGRAM_BLOCK={'gubun':'0','gubun1':'0','gubun2':'0','gubun3':'0','date':' ','time':' ','exchgubun':'K'}
PROGRAM_KEYS={'tot3':'program_amt','cha3':'arb_amt','bcha3':'nonarb_amt'}


def program_packet(body,received):
    """t1632 코스피 프로그램 당일 누적(gubun2=0) 최신 행 → 억원.

    2026-10-22: 원값(백만원) 전체 −329,420 · 차익 −6,322 · 비차익 −323,097이 키움 ka90005와 같았다.
    gubun2=1은 '직전 대비 증감'이라 쓰지 않는다. 응답 날짜가 오늘이 아니면(장 시작 전 전일분 등) 받지 않는다."""
    if str(body.get('rsp_cd',''))!='00000':raise ValueError('LS t1632 응답 오류 %s %s'%(body.get('rsp_cd'),body.get('rsp_msg')))
    day=str((body.get('t1632OutBlock') or {}).get('date','')).strip()
    if day and day!=received.strftime('%Y%m%d'):raise ValueError('LS t1632 날짜가 오늘이 아닙니다(%s)'%day)
    rows=body.get('t1632OutBlock1') or []
    if not rows:raise ValueError('LS t1632 시간대 행이 없습니다')
    latest=rows[0];at=row_time(latest.get('time'),received.date())
    if at is None:raise ValueError('LS t1632 시간 형식 확인 필요: %r'%latest.get('time'))
    if at>received+dt.timedelta(seconds=60):raise ValueError('LS t1632 데이터 시각이 현재보다 늦습니다(%s)'%at.time())
    values={key:v*.01 for f,key in PROGRAM_KEYS.items() for v in [latest.get(f)] if isinstance(v,(int,float)) and not isinstance(v,bool)}
    if not values:raise ValueError('LS t1632 응답에 프로그램 금액이 없습니다')
    return {'asof':at.isoformat(),'source':'LS t1632 · KRX 코스피','values':values,'quality':'장중 잠정','scope':'cumulative_day'}


CONNECT_TIMEOUT=15   # 연결 대기는 기존 그대로
RETRY_DELAYS=(30,60,120)
OPTION_PARTS=(('board','LS 옵션 전광판','t2301'),('investors','LS 옵션 투자자','t1602 시장구분 5·6'))


class FeedStopped(Exception):
    """수집 종료 요청. 오류/재시도 상태로 기록하지 않는다."""


class RequestGate:
    """같은 TR의 요청 시작 간격만 공유한다. 응답 대기 중에는 잠금을 잡지 않는다."""
    def __init__(self,stop,interval=1.1,clock=time.monotonic):
        self.stop=stop;self.interval=interval;self.clock=clock
        self.lock=threading.Lock();self.next={}

    def acquire(self,tr):
        while not self.stop.is_set():
            with self.lock:
                now=self.clock();delay=self.next.get(tr,now)-now
                if delay<=0:
                    self.next[tr]=now+self.interval
                    return
            if self.stop.wait(delay):break
        raise FeedStopped()


class OptionSchedule:
    """옵션 조회 한 갈래(전광판 t2301 / 투자자 합계 t1602 5·6)의 다음 조회 시각과 실패·재시도 상태.

    2026-10-29 13:34~14:23 t2301 읽기 15초 초과가 5분마다 10회가량 이어졌다(선물 t2111·프로그램 t1632는 정상).
    실패하면 다음 5분을 기다리지 않고 30초 → 60초 → 120초 뒤 다시 묻고, 그래도 안 되면 원래 주기로 돌아간다."""

    def __init__(self,poll):
        self.poll=poll;self.next=0.0;self.retry=0;self.last_ok=None;self.last_error=''

    def due(self,now):
        return now>=self.next

    def success(self,now,when):
        self.retry=0;self.next=now+self.poll;self.last_ok=when;self.last_error=''

    def failure(self,now,error):
        """다음 조회까지 초. 재시도 3회를 다 쓰면 원래 주기로 돌아가고 재시도 횟수를 다시 센다."""
        self.retry+=1;self.last_error=error
        if self.retry<=len(RETRY_DELAYS):
            delay=RETRY_DELAYS[self.retry-1]
        else:
            self.retry=0;delay=self.poll
        self.next=now+delay
        return delay


def option_log(directory,line):
    """옵션 TR별 응답 시간·실패 사유·재시도를 data/ls_option.log에 한 줄씩 남긴다(키·토큰 없음)."""
    try:
        with open(directory/'ls_option.log','a',encoding='utf-8') as f:f.write(now_kst().isoformat()[:19]+' '+line+'\n')
    except OSError:pass


def option_detail(name,st,now_at,timeouts,delay=None):
    """수신 상태 패널 문구. 실패면 지연 시간·마지막 성공·재시도·오늘 타임아웃 횟수를 함께."""
    last=st.last_ok
    lag='조회 지연 %d분 · 마지막 성공 %s'%((now_at-last).total_seconds()//60,last.strftime('%H:%M')) if last else '오늘 성공 기록 없음'
    retry=('재시도 %d/%d(%d초 뒤)'%(st.retry,len(RETRY_DELAYS),delay) if st.retry else '%d분 뒤 다시 조회'%(delay//60)) if delay else ''
    return ' · '.join(x for x in (lag,retry,'오늘 타임아웃 %d회'%timeouts,st.last_error[:120]) if x)


class LsFeed(threading.Thread):
    def __init__(self,cfg,engine,stop,extras=None):
        super().__init__(daemon=True);self.cfg=cfg;self.engine=engine;self.stop=stop
        self.access=None;self.expires=0
        self.token_lock=threading.Lock();self.gate=RequestGate(stop)
        self.option_deriv=None;self.option_worker=None;self.option_close_day=None
        self.worker_stop=threading.Event()
        self.futures_close_day=None;self.last_cycle=None
        # 감시기(supervisor)가 보는 heartbeat. 반복마다 갱신한다. stopped_by_config: 키 없음 등 의도한 종료.
        self.beat=time.monotonic();self.option_beat=time.monotonic();self.stopped_by_config=False
        self.sf_master_day=None;self.sf_months={};self.next_sf=0.0;self.sf_close_day=None;self.next_sf_close=0.0
        self.extras=extras or (lambda day:(None,None));self.next_close=0;self.close_tries={}
        self.keys=None;self.deriv=None;self.last_options=-1e9;self.next_after=0
        poll=(cfg.get('ls') or {}).get('option_poll_sec',300)
        self.opt={part:OptionSchedule(poll) for part,_,_ in OPTION_PARTS};self.opt_timeouts={};self.next_board=0.0

    def series(self,requests,access,gubun1,gubun2='0'):
        """시간대별 행 최대 500개(30초 간격, 최신 먼저). 마감 요약용."""
        block={'market':'4','upcode':'900','gubun1':gubun1,'gubun2':gubun2,'cts_time':'','cts_idx':0,'cnt':500,'gubun3':'','exchgubun':'K'}
        self.gate.acquire('t1602');self.check_stop()
        r=requests.post(BASE+'/stock/investor',timeout=15,json={'t1602InBlock':block},
            headers={'content-type':'application/json; charset=utf-8','authorization':'Bearer '+access,'tr_cd':'t1602','tr_cont':'N','tr_cont_key':''})
        if r.status_code==401:self.invalidate_token(access)
        self.check_stop()
        body=r.json()
        if str(body.get('rsp_cd',''))!='00000':raise ValueError('LS 응답 오류 %s %s'%(body.get('rsp_cd'),body.get('rsp_msg')))
        return body.get('t1602OutBlock1') or []

    def close_tick(self,requests,key,secret,at):
        """장 마감 후·휴장일에 마감 요약을 저장한다. 확정 추정이 되면 멈추고, 아니면 10분마다 다시 확인한다."""
        name='LS 마감 요약'
        day=cs.target_day(at,self.cfg)
        if day is None:
            self.engine.connection(name,'대기','장 마감(15:50) 후·장 시작 전(08:45 전) 자동 저장');return
        days=cs.load(self.engine.store.directory)
        rec=days.get(day.isoformat());prev=cs.previous_business_day(day,self.cfg)
        today_done=cs.is_final(rec,at,self.cfg)
        if today_done and prev.isoformat() in days:
            if 'futures' not in rec:
                # 확정 옵션 문장은 보존. 현재 기준 재계산은 화면에서 별도로 표시한다.
                updated=cs.add_extras(dict(rec),None,None,self.futures_close(day.isoformat(),rec))
                if updated.get('options')!=rec.get('options') or updated.get('futures')!=rec.get('futures'):cs.save(updated,self.engine.store.directory)
            self.engine.connection(name,'정상','%s 확정 추정 값 저장됨'%cs.day_label(rec['day']));return
        if time.monotonic()<self.next_close:return
        tries=self.close_tries.get(day,0)
        if tries>=24:return  # 같은 날 최대 24회
        # 확정 추정 전에는 10분, 확정 추정 뒤 18:15까지는 늦은 정정 확인용으로 30분 간격.
        confirmed=bool(rec and rec['status']=='확정 추정')
        self.close_tries[day]=tries+1;self.next_close=time.monotonic()+(1800 if confirmed else 600)
        access=self.token(requests,key,secret)
        # '금일'은 가장 최근 거래일, '전일'은 그 직전 거래일. 전일은 처음 한 번만 채운다.
        todo=([('0',day)] if not today_done else [])+([('1',prev)] if prev.isoformat() not in days else [])
        for gubun2,target in todo:
            q=self.series(requests,access,'1',gubun2);self.stop.wait(1.1)
            a=self.series(requests,access,'2',gubun2);self.stop.wait(1.1)
            record=cs.build_record(target,q,a,at.isoformat())
            if at.date()!=target and record['status'] in ('마감 전 값','마감 직전 값'):
                # 장 시작 전·휴장일에 받은 값이 완결된 하루가 아니면 날짜를 잘못 붙였을 수 있어 저장하지 않는다.
                self.engine.connection(name,'확인 필요','%s로 붙일 LS 값이 완결된 하루가 아닙니다(최신 %s)'%(cs.day_label(target.isoformat()),record['latest_time'][:5]))
                continue
            spot,options=self.extras(target.isoformat())
            cs.add_extras(record,spot,options,self.futures_close(target.isoformat(),record))
            cs.save(record,self.engine.store.directory)
        rec=cs.load(self.engine.store.directory).get(day.isoformat())
        self.engine.connection(name,'정상' if rec and rec['status']=='확정 추정' else '확정 대기',
            '%s %s · 마지막 변화 %s'%(cs.day_label(day.isoformat()),rec['status'] if rec else '없음',(rec or {}).get('last_change','')[:5]))

    def futures_close(self,day,record=None):
        path=(record or {}).get('foreign_path') or {}
        with self.engine.lock:
            row=self.engine.store.daily().get(day);ticks=self.engine.store.ticks(day) if path else None
        return cs.futures_close(self.engine.store.directory,row,day,ticks,(path.get('low_time'),path.get('high_time')))

    def check_stop(self):
        if self.stop.is_set() or self.worker_stop.is_set():raise FeedStopped()

    def invalidate_token(self,used):
        with self.token_lock:
            # 이전 요청의 늦은 401이 이미 갱신한 토큰을 폐기하지 않게 한다.
            if self.access==used:self.access=None

    def token(self,requests,key,secret):
        # 옵션·선물 스레드가 동시에 만료를 발견해도 발급은 한 번만 한다.
        with self.token_lock:
            self.check_stop()
            if self.access and time.monotonic()<self.expires:return self.access
            r=requests.post(BASE+'/oauth2/token',headers={'content-type':'application/x-www-form-urlencoded'},
                            data={'grant_type':'client_credentials','appkey':key,'appsecretkey':secret,'scope':'oob'},timeout=10)
            self.check_stop();body=r.json()
            if r.status_code!=200 or 'access_token' not in body:raise ValueError('LS 토큰 발급 실패 HTTP %s %s'%(r.status_code,body.get('error_description') or body.get('rsp_msg') or ''))
            self.access=body['access_token'];self.expires=time.monotonic()+min(int(body.get('expires_in',3600)),86400)-300
            return self.access

    def call(self,requests,path,tr,block,read_timeout=15):
        """토큰/호출 시작 제한만 공유. 옵션 응답 대기는 선물 흐름을 잠그지 않는다."""
        key,secret=self.keys;access=self.token(requests,key,secret)
        self.gate.acquire(tr);self.check_stop()
        r=requests.post(BASE+path,timeout=(CONNECT_TIMEOUT,read_timeout),json={tr+'InBlock':block},
            headers={'content-type':'application/json; charset=utf-8','authorization':'Bearer '+access,'tr_cd':tr,'tr_cont':'N','tr_cont_key':''})
        if r.status_code==401:self.invalidate_token(access)
        self.check_stop();body=r.json()
        if str(body.get('rsp_cd',''))!='00000':raise ValueError('LS %s 응답 오류 %s %s'%(tr,body.get('rsp_cd'),body.get('rsp_msg')))
        return body

    def program(self,requests):
        """키움 ka90005 대신 LS t1632로 코스피 프로그램 매매(config ls.program=true일 때)."""
        name='LS 프로그램'
        try:
            body=self.call(requests,'/stock/program','t1632',PROGRAM_BLOCK);received=now_kst()
            packet=program_packet(body,received)
            with self.engine.lock:self.engine.store.raw(received.isoformat(),'LS t1632',{'row':body['t1632OutBlock1'][0]})
            self.engine.ingest(packet,received)
            self.engine.connection(name,'정상','t1632 · 데이터 시각 %s · 비차익 %+.1f억'%(packet['asof'][11:19],packet['values'].get('nonarb_amt',0)))
        except (ValueError,KeyError,IndexError,requests.RequestException) as e:
            self.engine.connection(name,'수신 오류',str(e)[:160])
        self.stop.wait(1.1)

    def investor_board(self,requests):
        """영웅문 0251 같은 시장 × 투자자 매매현황(LS t1601, 통합 거래소, 금액)."""
        import investor_board as ib
        name='LS 투자자 종합'
        try:
            body=self.call(requests,'/stock/investor','t1601',ib.BLOCK);received=now_kst()
            board=ib.parse(body,received);ib.save(board,self.engine.store.directory)
            stock=board['markets'][0]['rows'];f=next(r for r in stock if r['code']=='17')
            self.engine.connection(name,'정상','t1601 통합 · 거래소 외국인 %+.0f억 · 수신 %s'%(f['net'] or 0,received.isoformat()[11:19]))
        except (ValueError,KeyError,IndexError,StopIteration,requests.RequestException) as e:
            self.engine.connection(name,'수신 오류',str(e)[:160])
        self.stop.wait(1.1)

    def stock_futures(self,requests):
        """관심종목(config rest.watchlist)의 주식선물 근월물 시세·미결제·베이시스. 마스터(t8401)는 하루 한 번."""
        import stock_futures as sf
        name='LS 주식선물';watch=self.cfg['rest'].get('watchlist') or []
        at=now_kst();today=at.date()
        try:
            if self.sf_master_day!=today:
                body=self.call(requests,'/futureoption/market-data','t8401',{'dummy':''})
                if str(body.get('rsp_cd',''))!='00000':raise ValueError('LS t8401 응답 오류 %s'%body.get('rsp_msg'))
                self.sf_months=sf.front_months(body.get('t8401OutBlock') or [],[s['code'] for s in watch],today)
                self.sf_master_day=today
            rows,errors=[],[]
            for s in watch:
                months=self.sf_months.get(s['code']) or []
                if not months:errors.append('%s: 상장 주식선물 없음'%s['name']);continue
                row=None
                for month,focode,_ in months:
                    body=self.call(requests,'/futureoption/market-data','t8402',{'focode':focode})
                    row=sf.parse(body,s['code'],s['name'],month,now_kst())
                    # 만기일 장 마감 뒤나 만기가 지난 월물이면 다음 월물로 넘어간다.
                    if row['expiry'] and (row['expiry']>today.strftime('%Y%m%d') or
                                          (row['expiry']==today.strftime('%Y%m%d') and at.strftime('%H:%M')<'15:45')):break
                rows.append(row)
            sf.save(rows,now_kst(),errors,self.engine.store.directory)
            self.engine.connection(name,'정상' if not errors else '일부 오류','t8402 근월물 %d종목 · 수신 %s%s'%(
                len(rows),now_kst().isoformat()[11:19],(' · '+errors[0]) if errors else ''))
            return True
        except (ValueError,KeyError,IndexError,TypeError,requests.RequestException) as e:
            self.engine.connection(name,'수신 오류',str(e)[:160])
            return False

    def ensure_deriv(self,requests):
        if self.deriv is None:
            from ls_deriv import Derivatives
            self.deriv=Derivatives(self.cfg,self.engine,lambda p,t,b:self.call(requests,p,t,b),lambda:self.stop.wait(1.1))
        return self.deriv

    def ensure_option_deriv(self,requests):
        # 종목 캐시·마감 상태 등 변경 가능한 Derivatives 상태는 작업 흐름마다 분리한다.
        if self.option_deriv is None:
            from ls_deriv import Derivatives
            self.option_deriv=Derivatives(self.cfg,self.engine,
                lambda p,t,b:self.call(requests,p,t,b),lambda:self.stop.wait(1.1))
            self.option_deriv.option_call=lambda p,t,b:self.timed_option_call(requests,p,t,b)
        return self.option_deriv

    def timed_option_call(self,requests,path,tr,block):
        """옵션 TR만 읽기 대기를 ls.option_timeout(기본 35초)으로. TR별 응답 시간과 실패를 로그에 남긴다."""
        limit=float((self.cfg.get('ls') or {}).get('option_timeout',35))
        started=time.monotonic();label=tr+(' 시장%s·%s'%(block['market'],'수량' if block.get('gubun1')=='1' else '금액') if tr=='t1602' else '')
        try:
            body=self.call(requests,path,tr,block,read_timeout=limit)
        except FeedStopped:raise
        except Exception as e:
            option_log(self.engine.store.directory,'%s 실패 %.1f초(한도 %g초) %s: %s'%(label,time.monotonic()-started,limit,type(e).__name__,str(e)[:200]))
            raise
        option_log(self.engine.store.directory,'%s 정상 %.1f초'%(label,time.monotonic()-started))
        return body

    def options_due(self,requests,at,now=None,clock=now_kst,ingest=True):
        """전광판·투자자 합계를 따로 조회·재시도한다. 하나가 실패해도 다른 하나는 갱신된다."""
        deriv=self.ensure_option_deriv(requests);now=time.monotonic() if now is None else now
        for part,name,trs in OPTION_PARTS:
            self.check_stop()
            st=self.opt[part]
            if not st.due(now):continue
            attempt='재시도 %d/%d'%(st.retry,len(RETRY_DELAYS)) if st.retry else '정기'
            started=time.monotonic()
            try:
                if part=='board':
                    snaps=deriv.option_board(at,clock,ingest=ingest)
                    detail='t2301 %s월물 콜 %d·풋 %d 행사가'%(snaps[0]['month'],len(snaps[0]['rows']),len(snaps[1]['rows']))
                else:
                    report=deriv.option_investors(clock)
                    detail='t1602 투자자 합계(5=풋·6=콜) · 데이터 %s'%(report['groups'][0].get('time') or '—')
            except (ValueError,KeyError,IndexError,requests.RequestException) as e:
                timeout=isinstance(e,requests.Timeout)
                day=clock().date().isoformat()
                if timeout:self.opt_timeouts[day]=self.opt_timeouts.get(day,0)+1
                st_retry_before=attempt
                delay=st.failure(now,('응답 %g초 초과'%float((self.cfg.get('ls') or {}).get('option_timeout',35)) if timeout else '')+' '+str(e)[:160])
                option_log(self.engine.store.directory,'[%s] %s 실패(%s) %.1f초 → %s'%(name,trs,st_retry_before,time.monotonic()-started,
                    '%d초 뒤 재시도 %d/%d'%(delay,st.retry,len(RETRY_DELAYS)) if st.retry else '재시도 소진, %d초 뒤 정기 조회'%delay))
                self.engine.connection(name,'수신 오류',option_detail(name,st,clock(),self.opt_timeouts.get(day,0),delay))
                continue
            recovered=st.retry or st.last_error
            st.success(now,clock())
            day=clock().date().isoformat()
            option_log(self.engine.store.directory,'[%s] %s 정상(%s) %.1f초%s'%(name,trs,attempt,time.monotonic()-started,' · 복구' if recovered else ''))
            self.engine.connection(name,'정상','%s · %.1f초 · %s%s'%(detail,time.monotonic()-started,st.last_ok.strftime('%H:%M'),
                ' · 오늘 타임아웃 %d회'%self.opt_timeouts[day] if self.opt_timeouts.get(day) else ''))

    def derivatives(self,requests,at):
        """빠른 흐름의 선물 시세만 조회한다. 옵션은 독립 스레드가 수집한다."""
        ls=self.cfg.get('ls') or {}
        if not ls.get('derivatives',True):return
        self.ensure_deriv(requests)
        try:
            snap=self.deriv.futures(at,now_kst)
            self.engine.connection('LS 선물 시세','정상','t2111 %s · %.2f · 수신 %s'%(snap['code'],snap['values']['fut_price'],snap['received_at'][11:19]))
        except (ValueError,KeyError,IndexError,requests.RequestException) as e:
            self.engine.connection('LS 선물 시세','수신 오류',str(e)[:160])

    def after_close_futures(self,requests,at):
        """선물 마감 시세는 빠른 흐름에서 저장. 느린 옵션 저장과 파일 쓰기 소유권도 분리."""
        if not (self.cfg.get('ls') or {}).get('derivatives',True):return
        day=last_trading_day(at,self.cfg)
        if at.date()==day and at.strftime('%H:%M')<'16:10':return
        if self.futures_close_day==day or time.monotonic()<self.next_after:return
        self.next_after=time.monotonic()+600
        try:
            snap=self.ensure_deriv(requests).futures(at,now_kst,ingest=False)
            self.futures_close_day=day
            self.engine.connection('LS 선물 시세','장외 저장','%s 거래일 · %s · %.2f'%(day,snap['code'],snap['values']['fut_price']))
        except (ValueError,KeyError,IndexError,requests.RequestException) as e:
            self.engine.connection('LS 선물 시세','수신 오류',str(e)[:160])

    def after_close(self,requests,at):
        """옵션 작업 흐름 전용. 16:10 이후 성공한 두 갈래를 보존하고 실패한 쪽만 재시도."""
        if not (self.cfg.get('ls') or {}).get('derivatives',True):return
        day=last_trading_day(at,self.cfg)
        if at.date()==day and at.strftime('%H:%M')<'16:10':return
        deriv=self.ensure_option_deriv(requests)
        if deriv.after_close_done==day:return
        if self.option_close_day!=day:
            self.option_close_day=day
            boundary=day.isoformat()+'T16:10'
            # 재시작 시 같은 거래일의 마감 뒤 자료가 있는 갈래는 재요청하지 않는다.
            from ls_deriv import load_report
            for part,_,_ in OPTION_PARTS:
                st=self.opt[part];saved=None
                try:
                    if part=='board':
                        snapshots=[json.loads((self.engine.store.directory/('snapshot_'+kind+'.json')).read_text(encoding='utf-8'))['snapshot'] for kind in ('calls','puts')]
                        if all(x.get('source')=='LS t2301' and boundary<=x['received_at']<=at.isoformat() for x in snapshots):
                            saved=min(x['received_at'] for x in snapshots)
                    else:
                        report=load_report(self.engine.store.directory) or {}
                        if report.get('data_day')==day.isoformat() and boundary<=report.get('generated_at','')<=at.isoformat():saved=report['generated_at']
                except (OSError,ValueError,KeyError,TypeError):pass
                if saved:st.last_ok=dt.datetime.fromisoformat(saved);st.next=float('inf')
                else:st.next=0;st.last_ok=None;st.retry=0;st.last_error=''
        self.options_due(requests,at,ingest=False)
        boundary=dt.datetime.combine(day,dt.time(16,10),tzinfo=KST)
        for st in self.opt.values():
            if st.last_ok and st.last_ok>=boundary:st.next=float('inf')
        if all(st.next==float('inf') for st in self.opt.values()):
            deriv.after_close_done=day
            self.option_status('장외 저장','%s 거래일 옵션 시세·투자자 합계 저장본 표시'%day)

    def option_loop(self,requests):
        """수신이 느린 옵션은 자체 스레드에서만 조회한다. 선물 반복 주기와 무관하다."""
        was_session=False
        try:
            while not self.stop.is_set() and not self.worker_stop.is_set():
                self.option_beat=time.monotonic()
                at=now_kst();active=in_session(at,self.cfg)
                try:
                    if active:
                        if not was_session:
                            for st in self.opt.values():st.next=0;st.retry=0
                        self.options_due(requests,at)
                    else:self.after_close(requests,at)
                except FeedStopped:raise
                except Exception as e:
                    # 예상 못 한 오류도 이 회차만 버리고 30초 뒤 계속한다(2026-11-04: 흐름이 끝나 장 마감까지 멈춤).
                    self.option_status('수신 오류','예상 못 한 오류 · 30초 뒤 계속: '+type(e).__name__+': '+str(e)[:160])
                    option_log(self.engine.store.directory,'옵션 흐름 예외 %s: %s'%(type(e).__name__,str(e)[:200]))
                    self.stop.wait(30)
                was_session=active
                self.stop.wait(1)
        except FeedStopped:pass

    def start_options(self,requests):
        if (self.cfg.get('ls') or {}).get('derivatives',True) and self.option_worker is None:
            self.option_worker=threading.Thread(target=self.option_loop,args=(requests,),name='LS-options',daemon=True)
            self.option_worker.start()

    def cycle_log(self,started,lag,outcome):
        interval=None if self.last_cycle is None else started-self.last_cycle
        self.last_cycle=started
        line='선물 회차 시작간격=%s 처리=%.2f초 데이터지연=%s 상태=%s'%(('첫 회차' if interval is None else '%.2f초'%interval),
            time.monotonic()-started,'미확인' if lag is None else '%.1f초'%lag,outcome)
        try:
            with open(self.engine.store.directory/'ls_fast.log','a',encoding='utf-8') as f:f.write(now_kst().isoformat()[:19]+' '+line+'\n')
        except OSError:pass

    def option_status(self,status,detail):
        """장외 저장 상태는 전광판·투자자 두 줄에 같이 쓴다(장중에 남은 '수신 오류'가 밤새 남지 않게)."""
        for _,name,_ in OPTION_PARTS:self.engine.connection(name,status,detail)

    def query(self,requests,access,gubun1):
        block={'market':'4','upcode':'900','gubun1':gubun1,'gubun2':'0','cts_time':'','cts_idx':0,'cnt':1,'gubun3':'','exchgubun':'K'}
        self.gate.acquire('t1602');self.check_stop()
        r=requests.post(BASE+'/stock/investor',timeout=10,json={'t1602InBlock':block},
            headers={'content-type':'application/json; charset=utf-8','authorization':'Bearer '+access,'tr_cd':'t1602','tr_cont':'N','tr_cont_key':''})
        if r.status_code==401:self.invalidate_token(access)
        self.check_stop()
        return r.json()

    def run(self):
        ls=self.cfg.get('ls') or {}
        name='LS 선물 투자자'
        try:
            import requests
            key,secret=credentials(self.cfg);self.keys=(key,secret)
        except (ImportError,OSError,ValueError) as e:
            self.stopped_by_config=True
            self.engine.connection(name,'미설정','LS 키 파일 확인: '+str(e)[:80]);return
        self.start_options(requests)
        try:
            self.fast_loop(requests,key,secret)
        except FeedStopped:pass
        finally:
            self.worker_stop.set()
            if self.option_worker:
                # 진행 중 HTTP는 강제 취소할 수 없다. 종료 대기는 제한하고, 늦은 응답은 check_stop에서 버린다.
                limit=float(ls.get('option_timeout',35))+CONNECT_TIMEOUT+12
                self.option_worker.join(timeout=limit)
                if self.option_worker.is_alive():self.option_status('종료 대기','진행 중 요청이 반환되면 종료합니다')

    def fast_loop(self,requests,key,secret):
        ls=self.cfg.get('ls') or {};name='LS 선물 투자자'
        while not self.stop.is_set():
            self.beat=time.monotonic()
            try:
                self.fast_once(requests,key,secret,ls,name)
            except FeedStopped:raise
            except Exception as e:
                # 예상 못 한 오류(DB 잠김 등)도 이 회차만 버리고 계속한다. 흐름이 끝나면 마지막 '정상'이 남아 멈춤을 가린다.
                self.engine.connection(name,'수신 오류','예상 못 한 오류 · 10초 뒤 계속: '+type(e).__name__+': '+str(e)[:160])
                try:
                    with open(self.engine.store.directory/'ls_fast.log','a',encoding='utf-8') as f:
                        f.write(now_kst().isoformat()[:19]+' 선물 흐름 예외 %s: %s\n'%(type(e).__name__,str(e)[:200]))
                except OSError:pass
                self.stop.wait(10)

    def fast_once(self,requests,key,secret,ls,name):
        """선물 흐름 한 회차(장외면 마감 처리). 다음 회차까지 기다리고 돌아온다."""
        if True:
            at=now_kst()
            if not in_session(at,self.cfg):
                self.engine.connection(name,'장외 대기','장중 %d초마다 t1602 조회'%ls.get('poll_sec',30))
                self.after_close_futures(requests,at)
                try:self.close_tick(requests,key,secret,at)
                except (ValueError,KeyError,IndexError,requests.RequestException) as e:
                    self.engine.connection('LS 마감 요약','수신 오류',str(e)[:160])
                # 마감 뒤 정정분까지 담도록 같은 거래일 18:30까지 10분마다 투자자 종합표를 다시 받는다.
                if ls.get('investor_board',True) and business_day(at.date(),self.cfg) and '15:50'<=at.strftime('%H:%M')<='18:30' \
                        and time.monotonic()>=self.next_board:
                    self.next_board=time.monotonic()+600;self.investor_board(requests)
                # 주식선물은 15:45 마감 값을 하루 한 번 받는다(재시작 뒤 장외에도 화면이 비지 않게).
                if ls.get('stock_futures',True) and business_day(at.date(),self.cfg) and at.strftime('%H:%M')>='15:50' \
                        and self.sf_close_day!=at.date() and time.monotonic()>=self.next_sf_close:
                    if self.stock_futures(requests):self.sf_close_day=at.date()
                    else:self.sf_close_day=None;self.next_sf_close=time.monotonic()+600
                self.stop.wait(10);return
            started=time.monotonic();lag=None;outcome='정상'
            try:
                access=self.token(requests,key,secret)
                for gubun1,keys in (('1',QTY_KEYS),('2',AMT_KEYS)):
                    body=self.query(requests,access,gubun1);received=now_kst()
                    packet=ls_packet(body,keys,received)
                    with self.engine.lock:self.engine.store.raw(received.isoformat(),'LS t1602 '+gubun1,{'row':body['t1602OutBlock1'][0]})
                    self.engine.ingest(packet,received)
                lag=(now_kst()-dt.datetime.fromisoformat(packet['asof'])).total_seconds()
                self.engine.connection(name,'정상','t1602 · 데이터 시각 %s (%d초 전)'%(packet['asof'][11:19],lag))
            except (ValueError,KeyError,IndexError,requests.RequestException) as e:
                outcome='수신 오류';self.engine.connection(name,outcome,str(e)[:160])
            if ls.get('program'):self.program(requests)
            if ls.get('investor_board',True):self.investor_board(requests)
            if ls.get('stock_futures',True) and time.monotonic()>=self.next_sf:
                self.next_sf=time.monotonic()+float(ls.get('stock_futures_sec',180));self.stock_futures(requests)
            self.derivatives(requests,at)
            self.cycle_log(started,lag,outcome)
            # 전체 회차 시작을 기준으로 주기를 맞춘다. 옵션 응답 시간은 여기에 들어오지 않는다.
            self.stop.wait(max(0.1,float(ls.get('poll_sec',30))-(time.monotonic()-started)))
