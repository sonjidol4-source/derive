"""로컬 대시보드, 기존 REST 수집 및 32비트 OCX 별도 프로세스."""
import argparse
import csv
import io
import json
import os
import secrets
import sqlite3
from types import SimpleNamespace
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlparse
from common import ROOT,config,atomic_json,now_kst,in_session
from engine import Engine
from rest_feed import RestFeed
from ls_feed import LsFeed
from investor_board import view as board_view
from stock_futures import view as stock_futures_view
from close_summary import LATE_SPOT,flow_close,spot_observed,view as close_view,refresh_pending_options
from positions import parse_positions
from importers import flow_packet
from investor_view import load_view
from option_analytics import FUT_KEYS,build as build_option_analytics,ls_view
from ls_deriv import load_report as load_ls_options
from version_guard import VERSION_EXIT,BUSY_EXIT,updater_running
from deriv_today import DerivTodayFeed,view as deriv_today_view
from investor_history import HistoryFeed
from review_metrics import compare_close,aggregate_events
from gap_recovery import RecoveryWorker,read_view as recovery_view,export_csv as recovery_csv
from supervisor import ExclusiveHTTPServer,Supervisor,claim_instance,mark_stale


class ReadOnlyStore:
    """미리보기에서는 Engine을 만들거나 SQLite를 쓰기 모드로 열지 않는다."""
    def __init__(self,directory):
        self.directory=directory
        self.db=sqlite3.connect((directory/'radar.sqlite3').as_uri()+'?mode=ro',uri=True,check_same_thread=False)
        self.db.row_factory=sqlite3.Row

    def events(self,limit=100000):
        rows=self.db.execute('SELECT payload,outcomes FROM events ORDER BY at DESC LIMIT ?',(limit,))
        return [{**json.loads(r['payload']),'outcomes':json.loads(r['outcomes'])} for r in rows]

    def event_count(self):
        return self.db.execute('SELECT count(*) FROM events').fetchone()[0]

    def close(self):self.db.close()

def ls_investor_view():
    """키움 OCX를 끈 경우 옵션 투자자 패널은 LS 시장구분 5(풋)·6(콜) 합계를 보여 준다(2026-10-22 한투와 대조)."""
    view=ls_view(load_ls_options(),None)
    if not view:return {'status':'unavailable','note':'LS 수집기가 장중 5분마다(장 마감 뒤 16:10 이후 1회) 옵션 투자자 합계를 조회하면 표시됩니다.'}
    return {'status':'ls',**view}

class Application:
    def __init__(self,mode):
        self.cfg=config();self.mode=mode;self.token=secrets.token_urlsafe(32)
        self.engine=(SimpleNamespace(store=ReadOnlyStore(ROOT/'data'),lock=threading.RLock()) if mode=='preview'
                     else Engine(self.cfg,ROOT/('data_demo' if mode=='demo' else 'data'),mode))
        self.stop=threading.Event();self.snapshot={};self.worker=None;self.snapshot_lock=threading.Lock()
        self.url='http://127.0.0.1:'+str(self.cfg['port'])
        self.archive=None;self.awaiting_update=False;self.version_restarts=0;self.updater_clear_since=None
        self.option_analytics={};self.option_marks={};self.close_spot_cache={}
        self.review_cache=None;self.review_cache_at=0;self.review_lock=threading.Lock()
        self.recovery=None
        saved=self.engine.store.directory/'latest.json'
        if mode in ('live','preview') and saved.exists():
            try:
                self.archive=json.loads(saved.read_text(encoding='utf-8'))
            except (ValueError,OSError):pass
        if mode=='preview':
            if not self.archive:raise ValueError('미리보기용 latest.json 저장본이 없습니다.')
            self.snapshot={**self.archive,'mode':'preview','session':False,
                           'connections':{'미리보기':{'status':'저장본','detail':'API 수집기를 실행하지 않았습니다.'}}}
            self.option_analytics=build_option_analytics(self.engine.store.directory,cfg=self.cfg,persist_history=False)

    def publish(self,snapshot):
        with self.snapshot_lock:
            self.snapshot=snapshot
            atomic_json(self.engine.store.directory/'latest.json',snapshot)

    def run(self):
        if self.mode=='preview':return
        if self.mode=='demo':
            from demo import populate
            # 새 데모 재생시 이전 run의 가상 이벤트/시계열을 지워 중복 평가를 막음.
            for table in ('ticks','events','daily','raw'):self.engine.store.db.execute('DELETE FROM '+table)
            self.engine.store.db.commit()
            for snap in populate(self.engine):
                if self.stop.is_set():return
                self.publish(snap);self.stop.wait(.5)
            return
        # 수집 흐름은 감시기가 띄운다. 죽거나(예외로 스레드 종료) 멈추면(heartbeat 초과) 새로 띄운다(2026-11-04 사고 대책).
        self.supervisor=Supervisor(self.engine,self.stop)
        if self.cfg['rest']['enabled']:self.supervisor.add('REST',lambda stop:RestFeed(self.cfg,self.engine,stop))
        if (self.cfg.get('ls') or {}).get('enabled'):
            def ls_started(feed):
                if self.recovery is None:
                    self.recovery=RecoveryWorker(self.cfg,self.engine,feed,self.stop);self.recovery.start()
                else:self.recovery.feed=feed  # 누락 복구는 새 흐름의 토큰·호출 간격을 쓴다
            self.supervisor.add('LS',lambda stop:LsFeed(self.cfg,self.engine,stop,extras=self.close_extras),
                beats=lambda f:{'LS':f.beat,**({'LS 옵션':f.option_beat} if f.option_worker else {})},on_start=ls_started)
        # 한투: 투자자별 선물·미니·월물/위클리 콜·풋(1분) + LS 옵션 조회본으로 행사가별 미결제 변화. 키가 없으면 '미설정'만 표시.
        if (self.cfg.get('kis') or {}).get('enabled',True):self.supervisor.add('한투',lambda stop:DerivTodayFeed(self.cfg,self.engine,stop))
        # LS t1665 일별 확정 투자자 이력(누적 포지션·기준 분포). 마감 뒤·다음 날 아침에 확정치로 다시 받는다.
        if (self.cfg.get('ls') or {}).get('enabled'):self.supervisor.add('LS 이력',lambda stop:HistoryFeed(self.cfg,self.engine,stop))
        if not self.cfg['ocx']['enabled']:
            self.engine.connection('키움 OCX','사용 안 함','선물·옵션 시세는 LS OPEN API로 받습니다(로그인 창 없음)')
        else:
            if sys.platform!='win32':self.engine.connection('OCX','미지원 환경','Windows의 기존 Open API+ 설치가 필요합니다')
            else:self.start_worker()
        positionfile=self.engine.store.directory/'positions.csv'
        if positionfile.exists():
            try:self.engine.positions=parse_positions(positionfile.read_text(encoding='utf-8-sig'))
            except ValueError:self.engine.connection('포지션 CSV','형식 오류')
        while not self.stop.is_set():
            # 계산·저장 중 예외가 나도 화면 갱신을 멈추지 않는다(멈추면 마지막 화면이 계속 '정상'으로 남는다).
            try:self.cycle()
            except Exception as e:self.engine.connection('화면 갱신','오류',type(e).__name__+': '+str(e)[:160])
            self.stop.wait(10)

    def cycle(self):
        self.watch_worker()
        try:self.supervisor.check()
        except Exception as e:self.engine.connection('감시','오류',type(e).__name__+': '+str(e)[:160])
        try:self.update_option_analytics()
        except Exception as e:self.engine.connection('옵션 분석','계산 오류',type(e).__name__+': '+str(e)[:160])
        current=self.engine.compute()
        current['connections']=mark_stale(current['connections'],now_kst(),self.cfg)
        if not current['history'] and not in_session(now_kst(),self.cfg) and self.archive:
            archived=dict(self.archive)
            archived['mode']='archive'
            # 수급 값은 보관본, 연결 상태는 지금 것(보관본의 'OCX 프로세스 종료' 같은 옛 상태를 보여 주지 않는다).
            archived['connections']=current['connections']
            # 누적은 보관본이 아니라 지금 계산(LS t1665 확정 이력)으로 바꾼다. 보관본의 옛 부분 누적이 남지 않게.
            archived['cumulative']=current['cumulative']
            archived['market_snapshots']={**(archived.get('market_snapshots') or {}),**self.saved_snapshots()}
            archived['verdict']={**archived['verdict'],'label':'장외 · 최근 거래일 '+archived['day'],
                'side':0,'guide':'마지막 거래일의 저장된 관측입니다. 현재 장중 신호로 사용하지 않습니다.'}
            with self.snapshot_lock:self.snapshot=archived
        else:
            # 재시작 직후 등 엔진에 스냅숏이 없으면 저장된 마지막 조회(LS 마감 뒤 저장본 포함)를 보여 준다.
            if not in_session(now_kst(),self.cfg):
                current['market_snapshots']={**self.saved_snapshots(),**(current.get('market_snapshots') or {})}
            self.publish(current)
            if current['history']:self.archive=current

    def saved_snapshots(self):
        """장외 화면용: 마지막으로 저장된 선물·옵션 조회(LS 장 마감 뒤 저장본 등)."""
        out={}
        for kind in ('futures','calls','puts'):
            try:out[kind]=json.loads((self.engine.store.directory/('snapshot_'+kind+'.json')).read_text(encoding='utf-8'))['snapshot']
            except (OSError,ValueError,KeyError):pass
        return out

    def close_extras(self,day):
        """마감 요약에 붙일 같은 날의 현물과 옵션 해석(날짜가 같을 때만).

        장중 마지막 기록이 15:15 전이거나 없으면 키움 REST ka10051을 그날 날짜로 한 번 조회해 채운다(검토 보완 1).
        2026-10-16: REST 마감 후 +3,214억 vs 0780 확정 +3,415억 → 단위는 같고 확정치와 소폭 차이.
        """
        with self.engine.lock:row=self.engine.store.daily().get(day)
        spot=row
        if not row or spot_observed(row)[11:16]<LATE_SPOT or spot_observed(row)[:10]!=day:
            spot=self.rest_close_spot(day,row) or row
        return spot,dict(self.option_analytics)

    def rest_close_spot(self,day,row):
        cached=self.close_spot_cache.get(day)
        if cached and (cached['value'] or time.monotonic()-cached['at']<1800):return cached['value']
        value=None
        if self.cfg['rest'].get('enabled'):
            try:
                from rest_feed import Client,read_env,find_env,spot_packet,SPOT_STEX
                client=Client(read_env(find_env(self.cfg)))
                data=client.query('ka10051',{'mrkt_tp':'0','amt_qty_tp':'0','base_dt':day.replace('-',''),'stex_tp':SPOT_STEX})
                values=spot_packet(data,now_kst())['values']
                value={**{k:v for k,v in values.items() if v is not None},'k200':(row or {}).get('k200'),
                       'asof':now_kst().isoformat(),'source':'REST 마감 후 조회 ka10051 · 통합(KRX+NXT)'}
                self.engine.connection('마감 현물','정상','%s 외국인 %+.0f억 (REST 마감 후 조회)'%(day,values.get('foreign_spot_amt') or 0))
            except Exception as e:  # 인증·네트워크·응답 형식 오류 모두 마감 요약을 막지 않는다
                self.engine.connection('마감 현물','수신 오류',str(e)[:120])
        self.close_spot_cache[day]={'value':value,'at':time.monotonic()}
        return value

    def update_option_analytics(self):
        now=now_kst()
        k200=self.engine.measurement('k200',now)
        spot,source=(k200['value'],'실시간 KOSPI200') if k200['fresh'] else (None,'')
        fut={key:m for key in FUT_KEYS.values() for m in [self.engine.measurement(key,now)] if m['fresh']}
        try:self.option_analytics=build_option_analytics(self.engine.store.directory,spot,source,fut,self.cfg,now)
        except (ValueError,KeyError,TypeError,OSError) as e:
            self.option_analytics={'status':'unavailable','note':'옵션 분석 계산 오류: '+str(e)}
        if self.option_analytics.get('mode')=='live' and spot:self.record_option_observations(self.option_analytics,spot,now)
        refresh_pending_options(self.engine.store.directory,self.option_analytics,now,self.cfg)

    def review(self):
        with self.review_lock:
            if self.review_cache is None or time.monotonic()-self.review_cache_at>30:
                with self.engine.lock:
                    events=self.engine.store.events(100000)
                    count=self.engine.store.event_count()
                self.review_cache={'comparison':compare_close(self.engine.store.directory),
                                   'validation':aggregate_events(events,total_count=count)}
                self.review_cache_at=time.monotonic()
            return self.review_cache

    def record_option_observations(self,A,spot,now):
        """방향이 바뀐 조회 회차와 지수가 가까운 저항·지지 후보를 넘은 순간을 관측 기록에 남긴다."""
        base={'source':'option_analytics','data_at':A['generated_at'],'spot_source':A['spot_source'],
              'rule_context':{'method':'option-observation-1','structure_method':(A.get('methodology') or {}).get('id'),
                              'near_band_pct':(A.get('methodology') or {}).get('near_band_pct')},
              'input_observations':[{'source':'option_analytics',
                  'at':A['generated_at'],'quality':A['mode'],'provenance':'observed'}]}
        for g in A['flows']:
            side=g['explain'].get('side')
            key=('opt',g['investor'])
            if side in (1,-1) and self.option_marks.get(key)!=side:
                d=g['delta']
                kind='옵션 %s %s 쪽'%(g['label'],'상승' if side>0 else '하락')
                facts=[g['explain']['text'],'키움 델타 %+.0f / BS %+.0f 선물 계약 상당(%s 조회)'%(d['kiwoom']['total'],d['bs']['total'],A['generated_at'][11:16])]
                if self.engine.observe(kind,side,now,facts,{**base,'investor':g['investor'],'delta':d,'flat':self.cfg['rules'].get('option_delta_flat',50)}):
                    self.option_marks[key]=side
            elif side==0:
                self.option_marks.pop(key,None)  # 중립으로 돌아오면 다음 방향 출현을 새 관측으로 본다
        st=A.get('structure') or {};last=self.option_marks.get('spot')
        for wall,word,side,crossed in [(st.get('near_put_wall'),'지지 후보 이탈',-1,lambda a,b,k:a>=k>b),
                                       (st.get('near_call_wall'),'저항 후보 돌파',1,lambda a,b,k:a<=k<b)]:
            if wall and last is not None and crossed(last,spot,wall['strike']):
                self.engine.observe('OI %s %g'%(word,wall['strike']),side,now,
                    ['KOSPI200 %.2f → %.2f, %s %g (미결제 %s)'%(last,spot,word.split()[0],wall['strike'],format(round(wall['oi']),','))],
                    {**base,'wall':wall,'structure_complete':st.get('complete')})
        self.option_marks['spot']=spot

    def start_worker(self):
        env=os.environ.copy();env['RADAR_URL']=self.url;env['RADAR_TOKEN']=self.token
        cmd=self.cfg['ocx']['python_command']+[str(ROOT/'ocx_worker.py')]
        try:self.worker=subprocess.Popen(cmd,cwd=str(ROOT),env=env)
        except OSError:self.engine.connection('OCX','실행 실패','연결설정에서 기존 32비트 Python 실행 경로를 지정하세요')

    def watch_worker(self):
        if self.worker and self.worker.poll() is not None:
            code=self.worker.returncode;self.worker=None
            if code==VERSION_EXIT and self.version_restarts<3:
                self.awaiting_update=True
                self.engine.connection('OCX','버전처리 대기','키움 팝업의 [확인]을 눌러 업데이트하세요. 업데이트 창이 닫히면 수집기를 자동으로 다시 시작합니다')
            elif code==VERSION_EXIT:
                self.engine.connection('OCX','버전처리 반복','업데이트가 3회 적용되지 않았습니다. KOA Studio 등 Open API 사용 프로그램을 닫고 다시 실행하세요')
            elif code==BUSY_EXIT:
                self.engine.connection('OCX','중복 실행','다른 수집기·투자자수급점검 창이 Open API를 사용 중입니다. 그 창을 닫고 다시 실행하세요')
            else:self.engine.connection('OCX','프로세스 종료','로그인·32비트 Python·OCX 설치 상태를 확인하세요')
        # opstarter가 닫힌 뒤 OPVersionUp이 파일을 교체하므로, 둘 다 30초 이상 없을 때만 다시 로그인한다.
        if not self.awaiting_update:return
        if updater_running():self.updater_clear_since=None;return
        if self.updater_clear_since is None:self.updater_clear_since=time.monotonic()
        if time.monotonic()-self.updater_clear_since>=30:
            self.awaiting_update=False;self.updater_clear_since=None;self.version_restarts+=1
            self.engine.connection('OCX','재시작','업데이트 창이 닫혀 수집기를 다시 시작합니다')
            self.start_worker()

    def ingest(self,data):
        if 'connection' in data:
            self.engine.connection(data['connection'],data.get('status',''),data.get('detail',''));return
        for packet in data.get('batch',[data]):self.engine.ingest(packet)

def handler_for(application):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def safe_origin(self):
            host=self.headers.get('Host','')
            origin=self.headers.get('Origin')
            return host=='127.0.0.1:'+str(application.cfg['port']) and (not origin or origin==application.url)
        def send(self,status,data,kind='application/json; charset=utf-8'):
            payload=data if isinstance(data,bytes) else json.dumps(data,ensure_ascii=False,allow_nan=False).encode()
            self.send_response(status);self.send_header('Content-Type',kind)
            self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
        def do_GET(self):
            if not self.safe_origin():self.send(403,{'error':'local only'});return
            path=urlparse(self.path).path
            if path=='/':self.send(200,(ROOT/'dashboard.html').read_bytes(),'text/html; charset=utf-8')
            elif path=='/api/recovery.csv':self.send(200,recovery_csv(application.engine.store.directory,now_kst().date()).encode('utf-8'),'text/csv; charset=utf-8')
            elif path=='/review_ui.js':self.send(200,(ROOT/'review_ui.js').read_bytes(),'text/javascript; charset=utf-8')
            elif path=='/chart_recovery.js':self.send(200,(ROOT/'chart_recovery.js').read_bytes(),'text/javascript; charset=utf-8')
            elif path=='/api/state':
                with application.snapshot_lock:data=application.snapshot
                data=dict(data)
                if application.mode in ('live','preview'):
                    data['recovery']=application.recovery.snapshot() if application.recovery else recovery_view(application.engine.store.directory,data.get('day',now_kst().date()))
                if application.mode in ('live','preview'):
                    data['investor_options']=load_view() if application.cfg['ocx']['enabled'] else ls_investor_view()
                    data['option_analytics']=application.option_analytics
                    data['close_summary']=close_view(application.engine.store.directory,cfg=application.cfg,current_options=application.option_analytics)
                    data['review']=application.review()
                    data['investor_board']=board_view(application.engine.store.directory,application.cfg)
                    data['stock_futures']=stock_futures_view(application.engine.store.directory,application.cfg)
                    try:data['flows_close']=flow_close(application.engine.store.directory,application.cfg)
                    except (OSError,ValueError,KeyError,TypeError):data['flows_close']=None
                    try:data['deriv_today']=deriv_today_view(application.engine.store.directory,application.cfg)
                    except (OSError,ValueError,KeyError,TypeError) as e:data['deriv_today']={'status':'unavailable','note':'계산 오류: '+str(e)[:120]}
                self.send(200,data)
            elif path=='/api/bootstrap':self.send(200,{'token':application.token,'config':application.cfg,'mode':application.mode})
            elif path=='/api/catalog':
                p=ROOT/'data'/'tr_catalog.json'
                self.send(200,json.loads(p.read_text()) if p.exists() else {'specs':[],'note':'Open API+ 로그인 후 설치된 공식 TR 명세를 표시합니다.'})
            elif path=='/api/events.csv':
                stream=io.StringIO();cols=['at','kind','price','return5_pct','return15_pct','return30_pct','return60_pct','mode']
                writer=csv.DictWriter(stream,fieldnames=cols);writer.writeheader()
                with application.engine.lock:events=application.engine.store.events(100000)
                for e in events:
                    writer.writerow({'at':e['at'],'kind':e['kind'],'price':e['price'],'mode':e.get('mode',''),
                        **{'return%d_pct'%m:e['outcomes'].get(str(m),{}).get('return_pct','') for m in (5,15,30,60)}})
                self.send(200,('\ufeff'+stream.getvalue()).encode(),'text/csv; charset=utf-8')
            else:self.send(404,{'error':'not found'})
        def do_POST(self):
            if application.mode=='preview':
                self.send(403,{'error':'저장본 미리보기에서는 입력·설정을 변경할 수 없습니다.'});return
            if not self.safe_origin():
                self.send(403,{'error':'레이더의 로컬 주소에서 다시 열어 주세요.','code':'origin_rejected'});return
            if self.headers.get('Authorization')!='Bearer '+application.token:
                self.send(401,{'error':'앱이 다시 시작되어 화면 연결을 갱신해야 합니다.','code':'session_expired'});return
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=1000000:raise ValueError('요청 크기를 확인하세요')
                data=json.loads(self.rfile.read(length))
                path=urlparse(self.path).path
                if path=='/api/recovery':
                    if application.mode!='live' or application.recovery is None:raise ValueError('실시간 실행에서 과거 자료를 복구할 수 있습니다.')
                    accepted=application.recovery.request(data.get('ranges'))
                    self.send(200,{'accepted':accepted,'message':'누락 구간 복구를 요청했습니다.' if accepted else '기존 복구 조회가 진행 중입니다.'});return
                elif path=='/internal/ingest':application.ingest(data)
                elif path=='/api/import':
                    if application.mode=='demo':raise ValueError('실데이터 가져오기는 실시간 모드에서 사용하세요')
                    if data.get('type')=='positions':
                        rows=parse_positions(data['text'])
                        application.engine.positions=rows
                        (application.engine.store.directory/'positions.csv').write_text(data['text'],encoding='utf-8-sig')
                    elif data.get('type') in ('flows','hts0780'):
                        source='영웅문 0780 · 수동 입력' if data['type']=='hts0780' else 'HTS CSV · 사용자 확인'
                        packet=flow_packet(data['text'],source,application.engine.fut_code)
                        accepted=application.engine.ingest(packet)
                        if not accepted:raise ValueError('당일 한국시간 asof만 입력할 수 있습니다')
                        # 수동 입력 원문은 사후 대조용으로 남긴다.
                        with open(application.engine.store.directory/'manual_flows.csv','a',encoding='utf-8-sig',newline='') as f:
                            csv.writer(f).writerow([now_kst().isoformat(),packet['asof'],source,json.dumps(packet['values'],ensure_ascii=False)])
                    else:raise ValueError('CSV 종류 확인 필요')
                    application.publish(application.engine.compute())
                elif path=='/api/sources':
                    if application.mode=='demo':raise ValueError('연결 설정은 실시간 모드에서 사용하세요')
                    sources=data['sources']
                    if not isinstance(sources,list) or len(sources)>12:raise ValueError('최대 12개 TR source')
                    from tr_schema import validate_source
                    p=ROOT/'data'/'tr_catalog.json'
                    if not p.exists():raise ValueError('Open API+ 로그인 후 공식 명세를 확인하세요')
                    catalog={s['trcode']:s for s in json.loads(p.read_text())['specs']}
                    import re
                    for src in sources:
                        if not re.fullmatch('[a-zA-Z0-9_-]{1,40}',src.get('id','')):raise ValueError('id는 영문·숫자·밑줄로 지정하세요')
                        validate_source(src,catalog[src['trcode'].lower()])
                    cfg=config();cfg['ocx']['sources']=sources;atomic_json(ROOT/'config.json',cfg)
                    application.cfg=cfg;application.engine.cfg=cfg
                else:self.send(404,{'error':'not found'});return
                self.send(200,{'ok':True})
            except (ValueError,KeyError,TypeError) as e:self.send(400,{'error':str(e)})
    return Handler

def main():
    p=argparse.ArgumentParser();modes=p.add_mutually_exclusive_group()
    modes.add_argument('--demo',action='store_true');modes.add_argument('--live',action='store_true');modes.add_argument('--preview',action='store_true')
    p.add_argument('--no-browser',action='store_true')
    # 원본은 바로가기·restart_radar.bat이 옵션 없이 실행하므로 기본값은 실제 수집. 저장본만 보려면 --preview.
    args=p.parse_args();mode='demo' if args.demo else 'preview' if args.preview else 'live'
    port=config()['port'];url='http://127.0.0.1:%d'%port
    # 2026-11-04: 두 번 실행돼 수집기 둘이 같은 키로 조회했다. 수집(실시간·데모)은 실행 잠금, 모든 모드는 포트 독점으로 막는다.
    if mode!='preview' and not claim_instance(port):
        print('파생 수급 레이더가 이미 실행 중입니다. 새로 수집하지 않고 기존 화면을 엽니다: '+url,flush=True)
        if not args.no_browser:webbrowser.open(url)
        sys.exit(3)
    app=Application(mode)
    try:server=ExclusiveHTTPServer(('127.0.0.1',app.cfg['port']),handler_for(app))
    except OSError:
        print('대시보드 포트 %d를 다른 프로그램이 쓰고 있어 시작하지 않았습니다. 기존 실행창을 종료하거나 config.json의 port를 바꾸세요.'%port,flush=True)
        sys.exit(4)
    thread=threading.Thread(target=app.run,daemon=True);thread.start()
    print('파생 수급 레이더 2.0: '+app.url,flush=True)
    print('이 창을 닫으면 대시보드 서버도 꺼집니다.',flush=True)
    if not args.no_browser:webbrowser.open(app.url)
    try:server.serve_forever(poll_interval=.5)
    except KeyboardInterrupt:pass
    finally:
        app.stop.set()
        if app.worker:app.worker.terminate()
        server.server_close();thread.join(3)

if __name__=='__main__':main()
