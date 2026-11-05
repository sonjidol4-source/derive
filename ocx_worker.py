import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import QTimer
from common import ROOT, now_kst, config, number, nearest_month, atomic_json, in_session, last_trading_day
from kiwoom import Kiwoom,OpenApiBusy,FUT_FIELDS,OPT_FIELDS
from version_guard import BUSY_EXIT
from tr_schema import catalog,read_spec,validate_source,extract_source,normalize_strikes
from quote_snapshot import futures_snapshot,options_snapshot
from investor_probe import RECORD,probe_spec
from investor_view import save_option_report

# opt50038 투자자구분 공식 코드. 사모펀드·국가 코드가 없어 기관계 합산은 하지 않는다.
OPTION_INVESTORS=[['foreign','외국인','09'],['individual','개인','08'],['financial','금융투자','01']]

class Worker:
    def __init__(self,url,token):
        self.url,self.token=url,token;self.cfg=config()
        self.kw=Kiwoom(log=lambda s:self.status('OCX',s),module_dir=self.cfg['ocx']['openapi_dir'])
        self.kw.on_real=self.real;self.kw.on_message=lambda tr,msg:self.status(tr,msg)
        self.fut='';self.month='';self.opts={};self.reference=None;self.last_sources=0;self.last_registration=0;self.last_raw={}
        self.pending={};self.pending_sources=set()
        self.option_pending=0;self.last_option_poll=0;self.listed_strikes=[]

    def send(self,packet):
        body=json.dumps(packet,ensure_ascii=False,allow_nan=False).encode()
        req=urllib.request.Request(self.url+'/internal/ingest',data=body,headers={'Content-Type':'application/json','Authorization':'Bearer '+self.token})
        try:urllib.request.urlopen(req,timeout=2).close()
        except Exception:pass

    def status(self,name,detail):
        print(name+': '+detail,flush=True)
        state='정상' if detail=='정상' else '등록 완료' if detail.endswith('등록') else '확인 필요'
        self.send({'connection':name,'status':state,'detail':detail})

    def start(self):
        self.status('OCX','로그인 대기 · 버전처리 안내가 나오면 수집기가 스스로 종료합니다. 그다음 팝업의 [확인]을 누르세요')
        if not self.kw.login():self.status('OCX','로그인 실패');return False
        path=self.kw.call('GetAPIModulePath()') or self.cfg['ocx']['openapi_dir']
        specs=catalog(path)
        atomic_json(ROOT/'data'/'tr_catalog.json',{'module_path':str(path),'specs':specs,'generated_at':now_kst().isoformat()})
        self.status('OCX','정상')
        self.register_futures()
        # 키움 API에는 선물 투자자 수급 TR이 없다(opt50039 사용 불가, opt10059 선물 코드 빈 응답). 선물은 영웅문 0780 수동 입력.
        self.status('투자자별 선물 수급','키움 API 미제공 · 대시보드에서 영웅문 0780 값을 입력하세요')
        self.poll_option_investors()
        return True

    def poll_option_investors(self):
        """opt50038 옵션 투자자별 수량을 당일(일자구분=1)·누적(2)으로 한 번씩 조회해 저장."""
        if self.option_pending:return
        self.last_option_poll=time.monotonic()
        module=self.kw.call('GetAPIModulePath()') or self.cfg['ocx']['openapi_dir']
        try:spec=probe_spec(module)
        except (OSError,ValueError) as e:self.status('옵션 투자자',str(e));return
        if str(self.kw.call('GetLoginInfo(QString)','GetServerGubun'))=='1':
            self.status('옵션 투자자','모의투자 서버입니다. 실서버 로그인 필요');return
        # 델타 환산·미결제 벽 계산용 전 행사가 시세(opt50021/22)를 투자자 조회 직전에 갱신(회차당 +2건).
        if self.month:self.snapshot_options()
        # 휴장일·장 시작 전에 오늘 날짜로 조회하면 0 응답이 와서 직전 거래일 데이터를 덮어쓴다 → 마지막 거래일로 조회.
        data_day=last_trading_day(now_kst(),self.cfg)
        day=data_day.strftime('%Y%m%d')
        plan=[(investor,label,code,scope,gubun) for investor,label,code in self.cfg['ocx'].get('option_investors',OPTION_INVESTORS)
              for scope,gubun in (('daily_net_trades','1'),('cumulative','2'))]
        results=[];self.option_pending=len(plan)
        for investor,label,code,scope,gubun in plan:
            def done(result,msg,investor=investor,label=label,scope=scope):
                rows=(result or {}).get(RECORD,[])
                results.append({'investor':investor,'label':label,'scope':scope,'received_at':now_kst().isoformat(),
                                'row_count':len(rows),'message':msg if result is not None else (msg or '응답 없음'),'response':result})
                self.option_pending-=1
                if self.option_pending:return
                save_option_report(results,data_day=data_day.isoformat())
                ok=sum(1 for r in results if r['response'])
                self.status('옵션 투자자','정상' if ok==len(results) else '%d/%d 응답 · 실패 항목은 저장 파일 참조'%(ok,len(results)))
            self.kw.request(spec,{'일자구분':gubun,'일자':day,'투자자구분':code,'수량금액구분':'1'},done)

    def request_quote(self,code,inputs,records,done,prev_next=0):
        module=str(self.kw.call('GetAPIModulePath()') or self.cfg['ocx']['openapi_dir'])
        try:
            spec=read_spec(Path(module)/'data'/(code+'.enc'))
            if set(inputs)!=set(spec['inputs']):raise ValueError('공식 입력 이름과 다릅니다')
            # Only market quote queries with verified installed definitions.
            if code not in ('opt50001','opt50021','opt50022'):raise ValueError('허용되지 않은 조회')
            spec['records']={name:[f for f in fields if f in spec['records'].get(name,[])] for name,fields in records.items()}
            self.kw.request(spec,inputs,done,prev_next)
        except (OSError,ValueError,KeyError) as e:self.status(code,str(e))

    def load_reference(self):
        if self.reference:return
        try:
            with urllib.request.urlopen(self.url+'/api/state',timeout=2) as response:state=json.load(response)
            row=next((q for q in state.get('quality',[]) if q['key']=='k200'),{})
            value=number(row.get('value'))
            if value and str(row.get('at','')).startswith(now_kst().date().isoformat()):self.reference=abs(value)
        except (OSError,ValueError,KeyError):pass

    def snapshot_futures(self):
        code=self.fut
        def done(result,msg):
            if result is None:self.status('선물 초기조회',msg);return
            try:snapshot=futures_snapshot(result,code,now_kst().isoformat())
            except ValueError as e:self.status('선물 초기조회',str(e));return
            atomic_json(ROOT/'data'/'snapshot_futures.json',{'snapshot':snapshot,'response':result})
            self.send({'market_snapshot':snapshot})
            if snapshot['values'].get('k200'):self.reference=snapshot['values']['k200']
            self.load_reference();self.register_options()
            self.status('선물 초기조회','정상')
        self.request_quote('opt50001',{'종목코드':code},{'선옵현재가정보':['현재가','미결제약정','미결제약정전일대비','시장베이시스','이론베이시스'],
            '선옵시간대별체결':['체결시간','코스피200','현재가n']},done)

    def snapshot_options(self):
        month=self.month
        fields=['종목코드','행사가','현재가','미결제약정','내재변동성','델타']
        for cp,code,record in [('C','opt50021','콜종목결제월별시세'),('P','opt50022','풋종목결제월별시세')]:
            self.request_quote(code,{'만기년월':month},{record:fields},self.option_pages_handler(cp,code,record,month,fields))

    def option_pages_handler(self,cp,code,record,month,fields):
        # 콜·풋마다 독립된 처리기. 반복문 안 클로저가 마지막(풋) 처리기를 가리키던 문제(검토 7번)를 막는다.
        pages=[]
        def done(result,msg):
                if result is None:self.status(code,msg);return
                pages.append(result.get(record,[]))
                # 연속 조회 표시가 오면 같은 처리기로 최대 4쪽까지 이어 받는다.
                if msg=='연속 조회 잔여' and len(pages)<4:
                    self.request_quote(code,{'만기년월':month},{record:fields},done,2);return
                merged={record:[row for page in pages for row in page]}
                try:snapshot=options_snapshot(merged,cp,month,now_kst().isoformat())
                except ValueError as e:self.status(code,str(e));return
                # 서버가 '다음 쪽 없음'이어도 200행에서 끊긴다(2026-10-15: 상장 750~1595 339개 중 750~1247.5만 수신).
                # 완전 여부는 상장 행사가 목록과 대조해서만 판단한다.
                listed=self.listed_strikes;got={round(r['strike'],2) for r in snapshot['rows']}
                missing=[s for s in listed if round(s,2) not in got]
                snapshot.update(pages=len(pages),listed_count=len(listed),missing_count=len(missing),
                    listed_range=[listed[0],listed[-1]] if listed else None,complete=bool(listed) and not missing)
                atomic_json(ROOT/'data'/('snapshot_'+snapshot['kind']+'.json'),{'snapshot':snapshot,'response':merged})
                self.send({'market_snapshot':snapshot})
                self.status(code,'정상' if snapshot['complete'] else '상장 %d개 중 %d개 수신 · 먼 행사가 누락'%(len(listed),len(listed)-len(missing)))
        return done

    def register_futures(self):
        code=str(self.kw.call('GetFutureCodeByIndex(int)',0) or '')
        if not code:self.status('선물','근월물 코드 없음');return
        if code!=self.fut:
            self.kw.remove('5001');self.fut=code
            self.kw.register('5001',[code],list(FUT_FIELDS))
            self.send({'source':'OCX 상품','asof':now_kst().isoformat(),'fut_code':code,'values':{}})
            self.status('선물','근월물 '+code+' 등록')
            self.snapshot_futures()

    def register_options(self):
        if not self.reference:return
        raw=str(self.kw.call('GetMonthList()') or '').split(';')
        month=nearest_month(raw,now_kst(),self.cfg)
        if not month:self.status('옵션','사용 가능한 월물 없음');return
        try:
            strikes,scale=normalize_strikes(str(self.kw.call('GetActPriceList()') or '').split(';'),self.reference)
        except ValueError as e:self.status('옵션',str(e));return
        i=min(range(len(strikes)),key=lambda i:abs(strikes[i]-self.reference));n=self.cfg['ocx']['strikes_each_side']
        selected=strikes[max(0,i-n):i+n+1]
        codes={}
        for strike in selected:
            for cp,tag in [(2,'C'),(3,'P')]:
                code=str(self.kw.call('GetOptionCode(QString, int, QString)','%.2f'%strike,cp,month) or '')
                if code:codes[code]={'code':code,'strike':strike,'cp':tag,'month':month}
        # 이 월물에 실제 상장된 행사가(로컬 마스터 조회, 서버 요청 아님). 시세 조회가 전부 받았는지 대조용.
        self.listed_strikes=sorted(s for s in strikes if self.kw.call('GetOptionCode(QString, int, QString)','%.2f'%s,2,month))
        if set(codes)==set(self.opts) and month==self.month:return
        self.kw.remove('5002');self.month=month;self.opts=codes
        self.kw.register('5002',list(codes),list(OPT_FIELDS))
        self.send({'asof':now_kst().isoformat(),'source':'OCX 옵션 등록','opt_month':month,'options':list(codes.values())})
        self.status('옵션','%s월물 %d종목 · 행사가 배율 %g'%(month,len(codes),scale))
        self.snapshot_options()

    def real(self,code,kind):
        at=now_kst();values={}
        if code==self.fut and kind in ('선물시세','선물이론가'):
            fields=FUT_FIELDS if kind=='선물시세' else {k:v for k,v in FUT_FIELDS.items() if k not in (10,197)}
            for fid,key in fields.items():
                v=number(self.kw.real(code,fid))
                if v is not None:values[key]=abs(v) if key in ('k200','fut_price') else v
            if values.get('k200'):self.reference=values['k200']
            self.pending[(code,kind)]={'source':'OCX '+kind,'asof':at.isoformat(),'fut_code':self.fut,'values':values}
        elif code in self.opts and kind in ('옵션시세','옵션이론가'):
            opt=dict(self.opts[code]);fields=OPT_FIELDS if kind=='옵션시세' else {k:v for k,v in OPT_FIELDS.items() if k!=10}
            for fid,key in fields.items():
                v=number(self.kw.real(code,fid))
                if v is not None:opt[key]=abs(v) if key=='price' else v
            self.pending[(code,kind)]={'source':'OCX '+kind,'asof':at.isoformat(),'opt_month':self.month,'options':[opt]}

    def tick(self):
        if self.pending:
            batch=list(self.pending.values());self.pending={}
            self.send({'batch':batch})
        self.cfg=config()
        if self.kw.call('GetConnectState()')!=1:
            self.status('OCX','접속 끊김. 수신값의 유효 시간이 지나면 판정에서 제외됩니다.');return
        self.register_futures()
        if time.monotonic()-self.last_registration>60:
            self.load_reference()
            self.register_options();self.last_registration=time.monotonic()
        # 6건(3투자자×당일·누적)을 기본 5분마다. 조회 과부하를 피하려 장중에만 반복.
        if in_session(now_kst(),self.cfg) and time.monotonic()-self.last_option_poll>=self.cfg['ocx'].get('option_investor_poll_sec',300):
            self.poll_option_investors()
        if not in_session(now_kst(),self.cfg) or time.monotonic()-self.last_sources<self.cfg['ocx']['poll_sec']:return
        self.last_sources=time.monotonic()
        for src in self.cfg['ocx']['sources']:
            if not src.get('enabled') or src['id'] in self.pending_sources:continue
            try:
                tr=src['trcode'].lower()
                module=str(self.kw.call('GetAPIModulePath()') or self.cfg['ocx']['openapi_dir'])
                root=Path(module)/'data'
                candidates=[root/(tr+'.enc'),root/(tr.upper()+'.enc')]
                path=next(p for p in candidates if p.is_file())
                spec=read_spec(path);validate_source(src,spec)
                values={k:str(v).replace('{FUT_CODE}',self.fut).replace('{TODAY}',now_kst().strftime('%Y%m%d')).replace('{OPT_MONTH}',self.month) for k,v in src['inputs'].items()}
                def done(result,msg,src=src):
                    self.pending_sources.discard(src['id'])
                    if result is None:self.status(src['id'],msg);return
                    vals=extract_source(src,result)
                    if not vals:self.status(src['id'],'응답 필드에 값 없음');return
                    at=now_kst()
                    self.send({'asof':at.isoformat(),'source':'OCX '+src['trcode'],'values':vals,'fut_code':self.fut,'quality':'장중 잠정','scope':src.get('scope','market')})
                    atomic_json(ROOT/'data'/('tr_result_'+src['id']+'.json'),{'asof':at.isoformat(),'trcode':src['trcode'],'result':result,'values':vals,'note':msg})
                    self.status(src['id'],'정상')
                self.pending_sources.add(src['id'])
                self.kw.request(spec,values,done)
            except (ValueError,KeyError,StopIteration,OSError) as e:self.status(src.get('id','TR'),str(e))

def main():
    app=QApplication(sys.argv)
    try:worker=Worker(os.environ['RADAR_URL'],os.environ['RADAR_TOKEN'])
    except OpenApiBusy as e:
        print('OCX: '+str(e),flush=True);sys.exit(BUSY_EXIT)
    if not worker.start():return
    timer=QTimer();timer.timeout.connect(worker.tick);timer.start(1000)
    worker.tick();app.exec_()

if __name__=='__main__':main()
