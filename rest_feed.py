"""기존 kiwoom-flow-radar-v1 조회 클라이언트를 확장. 같은 .env와 키 사용."""
import datetime as dt
import json
import os
import threading
import time
from pathlib import Path
from common import ROOT, KST, business_day, stamp, now_kst, number, in_session
from legacy_rest import KiwoomClient, KiwoomAPIError

def read_env(path):
    values={}
    if path and Path(path).is_file():
        source=Path(path)
        if source.suffix.lower()=='.json':
            try:
                pointers=json.loads(source.read_text(encoding='utf-8-sig'))
                if not isinstance(pointers,dict):raise ValueError()
                for name,field in [('KIWOOM_APP_KEY','app_key_file'),('KIWOOM_SECRET_KEY','secret_key_file')]:
                    target=pointers.get(field)
                    if not isinstance(target,str) or not target.strip():raise ValueError()
                    keyfile=Path(target).expanduser()
                    if not keyfile.is_absolute():keyfile=source.parent/keyfile
                    values[name]=keyfile.read_text(encoding='utf-8-sig').strip()
                    if not values[name]:raise ValueError()
            except (OSError,ValueError,TypeError):
                raise ValueError('기존 키움 인증 파일을 읽을 수 없습니다') from None
        else:
            for line in source.read_text(encoding='utf-8-sig').splitlines():
                line=line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                if line.startswith('export '):
                    line=line[7:]
                k,v=line.split('=',1)
                values[k.strip()]=v.strip().strip('"').strip("'")
    values.update({k:v for k,v in os.environ.items() if k.startswith('KIWOOM_')})
    return values

def find_env(cfg):
    candidates=[cfg['rest'].get('env_file'),os.getenv('KIWOOM_ENV_FILE'),ROOT/'.env',
        ROOT.parent/'kiwoom-flow-radar-v1'/'.env', ROOT.parent/'.env']
    return next((str(Path(p).resolve()) for p in candidates if p and Path(p).is_file()), '')

class Client(KiwoomClient):
    READ_PATHS={**KiwoomClient.READ_PATHS,'ka10051':'/api/dostk/sect','ka90005':'/api/dostk/mrkcond',
        'ka20001':'/api/dostk/sect','ka10063':'/api/dostk/mrkcond','ka10064':'/api/dostk/chart'}
    def __init__(self,env):
        mock=env.get('KIWOOM_USE_MOCK','false').lower() in ('1','true','yes') or env.get('KIWOOM_ENV')=='mock'
        super().__init__(env.get('KIWOOM_APP_KEY') or env.get('KIWOOM_APPKEY',''),
            env.get('KIWOOM_SECRET_KEY') or env.get('KIWOOM_SECRETKEY',''),env='mock' if mock else 'live',min_interval=.6)
        self.auth_day=None

    def query(self,api,body):
        day=now_kst().date()
        if self.auth_day!=day:
            self._token=None
            self.authenticate();self.auth_day=day
        return self._send(self.READ_PATHS[api],api,body)[0]

# 현물 투자자 ka10051 거래소: 3=통합(KRX+NXT). 영웅문 [0251] '통합'과 맞춘다(2026-10-27 통합 +8,300억 ↔ 0251 +8,315억, KRX만 +6,002억).
SPOT_STEX='3'

def spot_packet(payload, at):
    rows=payload.get('inds_netprps')
    if not isinstance(rows,list):
        raise ValueError('ka10051: 업종별순매수 배열이 없습니다')
    row=next((r for r in rows if str(r.get('inds_cd','')).split('_')[0]=='001'),None)
    if row is None:
        raise ValueError('ka10051: 종합 코스피 행이 없습니다')
    mapping={'foreign_spot_amt':'frgnr_netprps','inst_spot_amt':'orgn_netprps',
        'individual_spot_amt':'ind_netprps','financial_spot_amt':'sc_netprps'}
    values={k:number(row.get(v)) for k,v in mapping.items()}
    return {'asof':at.isoformat(),'source':'REST ka10051 · 통합(KRX+NXT) 코스피','quality':'장중 잠정','scope':'cumulative_day','values':values}

def program_packet(payload,at):
    rows=payload.get('prm_trde_trnsn')
    if not isinstance(rows,list) or not rows:
        raise ValueError('ka90005: 프로그램 응답이 비었습니다')
    valid=[]
    for row in rows:
        raw=str(row.get('cntr_tm','')).strip()
        if len(raw)==4 and raw.isdigit():
            raw+='00'
        if len(raw)!=6 or not raw.isdigit():
            continue
        try:
            t=dt.datetime.combine(at.date(),dt.time(int(raw[:2]),int(raw[2:4]),int(raw[4:]))).replace(tzinfo=KST)
        except ValueError:
            continue
        if t<=at+dt.timedelta(seconds=3):
            valid.append((t,row))
    if not valid:
        raise ValueError('ka90005: 확인 가능한 당일 체결시각이 없습니다')
    t,row=max(valid,key=lambda x:x[0])
    return {'asof':t.isoformat(),'source':'REST ka90005 · KRX 코스피','quality':'장중 잠정','scope':'cumulative_day',
        'values':{'nonarb_amt':number(row.get('ndiffpro_trde_netprps'),.01),
        'arb_amt':number(row.get('dfrt_trde_netprps'),.01),'program_amt':number(row.get('all_netprps'),.01)}}

def index_packet(payload,at,k200=False):
    values={}
    if k200:
        p=number(payload.get('cur_prc'))
        if p is not None:
            values['k200']=abs(p)
    else:
        up=number(payload.get('rising')); limitup=number(payload.get('upl'))
        down=number(payload.get('fall')); limitdown=number(payload.get('lst')); flat=number(payload.get('stdns'))
        if all(x is not None for x in (up,limitup,down,limitdown,flat)):
            total=up+limitup+down+limitdown+flat
            if total>0:
                values['breadth']=(up+limitup)/total*100
    return {'asof':at.isoformat(),'source':'REST ka20001','values':values,'quality':'장중 시세'}

# 종목별 투자자 장중 값은 거래소 가집계라 하루 몇 번만 바뀐다(2026-10-29 ka10064 행 시각 09:19·09:50·11:02·13:11·14:15).
# 그 사이에는 값이 그대로인 것이 정상이고, 14:15 뒤로는 장 마감 집계(ka10059)가 나올 때까지 같은 값이다.
# 같은 날 삼성전자 외국인: 가집계 14:15 −3,523억 → 장마감 집계 −1,395억. 그래서 장 마감 뒤에는 ka10059로 바꾼다.
CLOSE_STOCKS=('15:50','20:30')  # 장 마감 집계를 다시 받는 구간(10분 간격)
NXT_FINAL='20:05'               # NXT 집계 반영 시각. 그 전 통합(_AL) 요청 값은 KRX 값과 같았다(10/29 확인).
CLOSE_STOCKS_SEC=600


def stock_intraday(data):
    """ka10064 장중 투자자 차트 → 마지막 가집계 시각의 외국인·기관계(억원)와 직전 가집계 대비."""
    rows=data.get('opmr_invsr_trde_chart')
    if not isinstance(rows,list):raise ValueError('ka10064 장중 투자자 배열 없음')
    by_time=sorted({str(r.get('tm','')).zfill(6):r for r in rows if str(r.get('tm','')).strip().isdigit()}.items())
    # 09:00 행은 값이 모두 0인 자리표시다. 첫 가집계 전에는 값을 비운다(0으로 보이지 않게).
    by_time=[(t,r) for t,r in by_time if t>'090000' or any(number(v) for k,v in r.items() if k!='tm')]
    if not by_time:return {'foreign_amt':None,'inst_amt':None,'agg_time':None,'basis':'첫 가집계(09:30 외국인) 전'}
    t,last=by_time[-1];prev=by_time[-2][1] if len(by_time)>1 else None
    f,i=(None if v is None else round(v,2) for v in (number(last.get('frgnr_invsr'),.01),number(last.get('orgn'),.01)))
    pf=number(prev.get('frgnr_invsr'),.01) if prev else None
    hm='%s:%s'%(t[:2],t[2:4])
    return {'foreign_amt':f,'inst_amt':i,'agg_time':hm,'foreign_change':None if f is None or pf is None else round(f-pf,2),
            'basis':'거래소 가집계 %s 기준 · 다음 가집계까지 값 고정'%hm}


def stock_close(data,day,at):
    """ka10059 종목별 투자자 일별(통합 _AL 요청) 중 그날 행. 없으면 None(아직 집계 전)."""
    rows=data.get('stk_invsr_orgn')
    if not isinstance(rows,list):raise ValueError('ka10059 일별 투자자 배열 없음')
    row=next((r for r in rows if str(r.get('dt',''))==day.strftime('%Y%m%d')),None)
    if not row:return None
    nxt=at.strftime('%H:%M')>=NXT_FINAL
    f,i=(None if v is None else round(v,2) for v in (number(row.get('frgnr_invsr'),.01),number(row.get('orgn'),.01)))
    return {'foreign_amt':f,'inst_amt':i,'closing':True,
            'basis':'장 마감 집계 · '+('통합(KRX+NXT)' if nxt else 'NXT 반영 전(20:05 뒤 다시 조회)')}


def ls_program(cfg):
    ls=cfg.get('ls') or {}
    return bool(ls.get('enabled') and ls.get('program'))

class RestFeed(threading.Thread):
    def __init__(self,cfg,engine,stop):
        super().__init__(daemon=True);self.cfg=cfg;self.engine=engine;self.stop=stop;self.client=None;self.last_stocks=0;self.next_close_stocks=0
        self.beat=time.monotonic();self.stopped_by_config=False

    def run(self):
        cfg=self.cfg
        try:
            import requests
        except ImportError:
            self.stopped_by_config=True
            self.engine.connection('REST','라이브러리 미설치','0_설치.bat 또는 pip install -r requirements.txt를 실행하세요')
            return
        try:
            self.client=Client(read_env(find_env(cfg)))
        except ValueError:
            self.stopped_by_config=True
            self.engine.connection('REST','미설정','기존 .env 경로를 연결설정에서 지정하세요. 새 키 발급은 필요하지 않습니다.')
            return
        if self.client.env=='mock':
            self.stopped_by_config=True
            self.engine.connection('REST','모의서버','시장 분석은 기존 실서버 키 설정으로 실행하세요. 가상 동작 확인은 데모실행.bat에서 가능합니다.')
            return
        self.engine.connection('REST','연결 중')
        while not self.stop.is_set():
            self.beat=time.monotonic()
            try:self.once(cfg)
            except Exception as e:
                # 네트워크·DB 잠김 등 예상 못 한 오류도 이 회차만 버리고 계속한다(스레드가 끝나면 화면엔 마지막 '정상'이 남는다).
                self.engine.connection('REST','수신 오류','예상 못 한 오류 · 30초 뒤 계속: '+type(e).__name__+': '+str(e)[:160])
                self.stop.wait(30)

    def once(self,cfg):
        at=now_kst()
        if not in_session(at,cfg):
            self.engine.connection('REST','장외 대기','장중 자동 조회')
            if business_day(at.date(),cfg) and CLOSE_STOCKS[0]<=at.strftime('%H:%M')<=CLOSE_STOCKS[1] \
                    and time.monotonic()>=self.next_close_stocks:
                self.next_close_stocks=time.monotonic()+CLOSE_STOCKS_SEC;self.capture_stocks(closing=True)
            self.stop.wait(10);return
        tasks=[('ka10051',{'mrkt_tp':'0','amt_qty_tp':'0','base_dt':at.strftime('%Y%m%d'),'stex_tp':SPOT_STEX},spot_packet),
            ('ka90005',{'date':at.strftime('%Y%m%d'),'amt_qty_tp':'1','mrkt_tp':'P00101','min_tic_tp':'1','stex_tp':'1'},program_packet),
            ('ka20001',{'mrkt_tp':'2','inds_cd':'201'},lambda d,t:index_packet(d,t,True)),
            ('ka20001',{'mrkt_tp':'0','inds_cd':'001'},index_packet)]
        if ls_program(cfg):
            # 프로그램은 LS t1632가 받는다(10/22 원값 일치 확인). config ls.program=false면 키움으로 되돌린다.
            tasks=[t for t in tasks if t[0]!='ka90005']
        success=0
        for api,body,fn in tasks:
            if self.stop.is_set():break
            try:
                data=self.client.query(api,body);received=now_kst()
                packet=fn(data,received)
                with self.engine.lock:
                    self.engine.store.raw(received.isoformat(),api,data)
                self.engine.ingest(packet,received);success+=1
            except (KiwoomAPIError,ValueError) as e:
                self.engine.connection(api,'수신 오류',str(e))
                self.engine.connection('REST','일부 수신 오류','설정·서버 응답을 확인하세요')
            else:
                self.engine.connection(api,'정상')
        if success==len(tasks):
            self.engine.connection('REST','정상','기존 키 인증 · KRX 현물'+(' (프로그램은 LS)' if ls_program(cfg) else '/프로그램'))
        if time.monotonic()-self.last_stocks>=cfg['rest']['stocks_poll_sec']:
            self.capture_stocks();self.last_stocks=time.monotonic()
        self.stop.wait(cfg['rest']['poll_sec'])

    def capture_stocks(self,closing=False):
        """장중: ka10064(종목별 장중 투자자 가집계). 장 마감 뒤: ka10059 그날 행, 아직 없으면 가집계 그대로."""
        watch=self.cfg['rest']['watchlist'];at=now_kst()
        try:
            out=[]
            for stock in watch:
                info=self.client.query('ka10001',{'stk_cd':stock['code']})
                row={**stock,'price':abs(number(info.get('cur_prc')) or 0),'change_pct':number(info.get('flu_rt'))}
                flow=None
                if closing:
                    data=self.client.query('ka10059',{'dt':at.strftime('%Y%m%d'),'stk_cd':stock['code']+'_AL',
                                                      'amt_qty_tp':'1','trde_tp':'0','unit_tp':'1'})
                    with self.engine.lock:self.engine.store.raw(now_kst().isoformat(),'ka10059 '+stock['code'],data)
                    flow=stock_close(data,at.date(),at)
                if flow is None:
                    data=self.client.query('ka10064',{'mrkt_tp':'000','amt_qty_tp':'1','trde_tp':'0','stk_cd':stock['code']})
                    with self.engine.lock:self.engine.store.raw(now_kst().isoformat(),'ka10064 '+stock['code'],data)
                    flow=stock_intraday(data)
                    if closing:flow['basis']+=' · 장 마감 집계 전'
                out.append({**row,**flow})
            self.engine.ingest({'asof':now_kst().isoformat(),'source':'REST 관심종목','stocks':out})
            self.engine.connection('관심종목','정상','장 마감 집계(ka10059)' if closing else '장중 가집계(ka10064)')
        except (KiwoomAPIError,ValueError) as e:
            self.engine.connection('관심종목','수신 오류',str(e))
