import datetime as dt
import threading
import uuid
from common import LABELS, stamp, now_kst, number, in_session, expected_days, previous_futures_expiry, monthly_expiry
from positions import scenarios
from store import Store
from review_metrics import event_metadata, provenance
from flow_explanation import build_explanation, NAMES
from rebound import rebound_view

from price_oi import price_oi_quadrant, reading, rules as price_oi_rules

QUOTE_KEYS=('k200','fut_price','fut_prev_close','fut_oi','fut_oi_prev','basis','basis_theory','basis_residual','breadth')

def next_futures_expiry(day, cfg):
    for ahead in range(4):
        ym=day.year*12+day.month-1+ahead
        y,m0=divmod(ym,12)
        if m0+1 in (3,6,9,12):
            expiry=monthly_expiry('%04d%02d'%(y,m0+1),cfg)
            if expiry>=day:
                return expiry
    return None

CONFIRMED_KEYS=('foreign_fut_qty','inst_fut_qty','finalized','quality','confirmed_source','confirmed_at')

class Engine:
    def __init__(self, cfg, directory, mode='live'):
        self.cfg, self.mode = cfg, mode
        self.store = Store(directory)
        self.lock = threading.RLock()
        self.day = None
        self.values = {}
        self.manual_prev = {}
        self.options = {}
        self.market_snapshots = {}
        self.positions = []
        self.history = []
        self.fut_code = ''
        self.opt_month = ''
        self.segment = ''
        self.last_tick_minute = None
        self.candidate = None
        self.last_event = {}
        self.connections = {}
        self.stocks = []
        self.calendar_warning = ''
        self.rebound_candidate = None

    def new_day(self, at):
        day = stamp(at).date().isoformat()
        if day == self.day:
            return
        self.day = day
        self.values, self.options, self.stocks = {}, {}, []
        self.manual_prev = {}
        self.market_snapshots = {}
        self.fut_code, self.opt_month = '', ''
        self.candidate = None
        self.rebound_candidate = None
        self.last_tick_minute = None
        self.history = self.store.ticks(day)
        self.segment = ''
        # 재시작은 같은 날짜의 수신값만 복구. freshness는 원래 수신 시각을 사용.
        if self.history:
            tail = self.history[-1]
            self.values = {key:dict(row) for key,row in tail['values'].items()}
            self.fut_code = tail.get('fut_code','')
            self.segment = tail.get('segment','')
            self.last_tick_minute = tail['at'][:16]
        self.calendar_warning = '' if int(self.day[:4])==self.cfg['calendar']['verified_year'] else '거래소 휴장일 설정의 연도를 갱신하세요. 만기 누적 완전성은 보류합니다.'

    def ingest(self, packet, clock=None):
        clock = stamp(clock or now_kst())
        with self.lock:
            self.new_day(clock)
            if 'market_snapshot' in packet:
                snapshot=packet['market_snapshot']
                if snapshot.get('kind') not in ('futures','calls','puts'):return False
                received=stamp(snapshot['received_at'])
                if received.date()!=clock.date() or received>clock+dt.timedelta(seconds=3):return False
                self.market_snapshots[snapshot['kind']]=snapshot
                return True
            at = stamp(packet.get('asof',clock.isoformat()))
            if at.date().isoformat()!=self.day or at > clock+dt.timedelta(seconds=3):
                return False
            code = packet.get('fut_code')
            if code and code != self.fut_code:
                self.fut_code = code
                self.segment = code + ':' + at.isoformat()
                self.candidate = None
                for key in ['fut_price','fut_prev_close','fut_oi','fut_oi_prev','basis','basis_theory','basis_residual','foreign_fut_qty','inst_fut_qty','individual_fut_qty','financial_fut_qty',
                            'foreign_fut_amt','inst_fut_amt','individual_fut_amt','financial_fut_amt']:
                    self.values.pop(key,None)
            source = packet.get('source','unknown')
            for key, val in packet.get('values',{}).items():
                if key not in LABELS:
                    continue
                v = number(val)
                if v is None:
                    continue
                old = self.values.get(key)
                if old and stamp(old['at'])>at:
                    continue
                if str(packet.get('quality','')).startswith('사용자 입력') and self.manual(old) and stamp(old['at'])<at:
                    # 직전 수동 입력과의 차이만 '입력 간격' 그대로 보여 준다.
                    self.manual_prev[key]={'value':old['value'],'at':old['at']}
                if old and old.get('source','').startswith(('OCX','LS')) and source.startswith('REST') and (clock-stamp(old['at'])).total_seconds()<45 and key=='k200':
                    continue
                self.values[key] = {'value':v,'at':at.isoformat(),'source':source,
                    'quality':packet.get('quality','provisional'),'scope':packet.get('scope','market')}
            if packet.get('opt_month') and packet['opt_month'] != self.opt_month:
                self.opt_month = packet['opt_month']
                self.options = {}
            for opt in packet.get('options',[]):
                if opt.get('cp') not in ('C','P') or number(opt.get('strike')) is None:
                    continue
                code = opt.get('code')
                row = self.options.setdefault(code, {})
                row.update({k:opt[k] for k in ('code','strike','cp','month') if k in opt})
                for key in ('price','oi','iv','delta'):
                    v=number(opt.get(key))
                    if v is not None:
                        row[key]={'value':v,'at':at.isoformat(),'source':source}
            if 'stocks' in packet:
                self.stocks = [{**r,'asof':at.isoformat()} for r in packet['stocks']]
            return True

    def connection(self, name, status, detail=''):
        with self.lock:
            self.connections[name]={'status':status,'detail':detail,'at':now_kst().isoformat()}

    @staticmethod
    def manual(row):
        return bool(row) and str(row.get('quality','')).startswith('사용자 입력')

    def flow_ttl(self, row):
        # 영웅문 화면 값을 사람이 옮겨 적는 수급은 갱신 간격이 길어 '마지막 값 표시'용 유효 시간을 따로 둔다.
        if self.manual(row):
            return self.cfg['quality'].get('manual_flow_ttl_sec', 900)
        return self.cfg['quality']['flow_ttl_sec']

    def measurement(self, key, at):
        row = self.values.get(key)
        if not row:
            return {'key':key,'label':LABELS[key][0],'unit':LABELS[key][1],'value':None,'fresh':False,'status':'미연결','age_sec':None}
        age = (stamp(at)-stamp(row['at'])).total_seconds()
        quote = key in QUOTE_KEYS
        ttl = self.cfg['quality']['quote_ttl_sec'] if quote else self.flow_ttl(row)
        fresh = 0 <= age <= ttl and stamp(row['at']).date().isoformat()==self.day
        return {'key':key,'label':LABELS[key][0],'unit':LABELS[key][1],**row,'fresh':fresh,
            'status':'정상' if fresh else '수신 지연','age_sec':round(age,1)}

    def val(self,key,at):
        m=self.measurement(key,at)
        return m['value'] if m['fresh'] else None

    @staticmethod
    def _window_identity(key, values):
        def identity(row):
            source=str(row.get('source',''))
            # Keep the existing same-LS recovery policy; metadata still records mixed provenance.
            return ('LS' if source.startswith('LS') else source,row.get('scope'))
        result=(identity(values.get(key,{})),)
        if key=='basis_residual':
            result+=tuple(identity(values.get(k,{})) for k in ('basis','basis_theory'))
        return result

    def _point_info(self, key, target, end, endpoint=None):
        """Search valid predecessors within the existing tolerance, then validate the whole window."""
        target,end=stamp(target),stamp(end)
        missing=lambda code,reason:{'status':code,'reason':reason,'row':None}
        tol=self.cfg['quality']['window_tolerance_sec']
        quote=key in ('k200','fut_price','fut_oi','basis','basis_residual')
        segmented=key.endswith('fut_qty') or key in ('fut_oi','basis','basis_residual','fut_price')
        choices=[h for h in self.history if stamp(h['at']).date()==target.date()
                 and 0 <= (target-stamp(h['at'])).total_seconds() <= tol]
        chosen=None
        for h in reversed(choices):
            row=h['values'].get(key)
            if not row or not row.get('at') or self.manual(row):continue
            source_at=stamp(row['at'])
            ttl=self.cfg['quality']['quote_ttl_sec'] if quote else self.flow_ttl(row)
            if source_at.date()!=target.date() or not 0 <= (stamp(h['at'])-source_at).total_seconds() <= ttl:continue
            if segmented and h.get('segment','')!=self.segment:continue
            chosen=h;break
        if chosen is None:
            return missing('no_baseline','허용 범위 안에 유효한 과거 기준점이 없습니다.')
        row=chosen['values'][key]
        expected=self._window_identity(key,chosen['values'])
        chain=[h for h in self.history if stamp(chosen['at'])<=stamp(h['at'])<=end]
        if endpoint is not None:
            chain.append({'at':end.isoformat(),'values':endpoint,'segment':self.segment})
        observed_times=[];source_times=[]
        for h in chain:
            v=h['values'].get(key)
            if segmented and h.get('segment','')!=self.segment:
                return missing('contract_changed','비교 구간에 월물 전환이 있습니다.')
            if v and self.manual(v):return missing('manual','비교 구간에 수동 입력이 있습니다.')
            if v and self._window_identity(key,h['values'])!=expected:
                return missing('source_changed','비교 구간의 출처·집계 범위가 바뀌어 연결하지 않습니다.')
            ttl=self.cfg['quality']['quote_ttl_sec'] if quote else self.flow_ttl(v or row)
            if not v or not v.get('at') or not 0 <= (stamp(h['at'])-stamp(v['at'])).total_seconds() <= ttl:continue
            observed_times.append(stamp(h['at']));source_times.append(stamp(v['at']))
        observed_times.append(end)
        gap=self.cfg['quality']['max_gap_sec']
        if any((b-a).total_seconds()>gap for a,b in zip(observed_times,observed_times[1:])):
            return missing('gap','유효 관측 사이의 공백이 허용 범위를 초과합니다.')
        if any(b<a or (b-a).total_seconds()>gap for a,b in zip(source_times,source_times[1:])):
            return missing('source_gap','원자료 시각의 역행 또는 긴 수신 공백이 있습니다.')
        return {'status':'ready','reason':'계산 가능','row':row,'values':chosen['values'],
                'base_observed_at':chosen['at'],'fallback_used':chosen is not choices[-1]}

    def _point(self, key, target, end):
        result=self._point_info(key,target,end)
        return result['row']['value'] if result['status']=='ready' else None

    def delta(self, key, minutes, at, offset=0):
        at=stamp(at)
        end=at-dt.timedelta(minutes=offset)
        target=end-dt.timedelta(minutes=minutes)
        if offset:
            current_info=self._point_info(key,end,end)
            current=current_info['row'] if current_info['status']=='ready' else None
            endpoint=current_info.get('values')
        else:
            measurement=self.measurement(key,at)
            current=measurement if measurement['fresh'] and not self.manual(measurement) else None
            endpoint=self.values
        result={'value':None,'status':'current_stale','reason':'현재 자료 미수신·지연 또는 수동 입력입니다.',
                'requested_minutes':minutes,'target_at':target.isoformat()}
        if current is not None:
            base=self._point_info(key,target,end,endpoint=endpoint)
            result.update({k:v for k,v in base.items() if k not in ('row','values')})
            if base['status']=='ready':
                result.update(value=current['value']-base['row']['value'],start_at=base['row']['at'],
                              end_at=current['at'],base_source=base['row'].get('source'),current_source=current.get('source'),
                              actual_minutes=(stamp(current['at'])-stamp(base['row']['at'])).total_seconds()/60)
        if not offset:
            if not hasattr(self,'window_details'):self.window_details={}
            self.window_details.setdefault(key,{})['m%d'%minutes]=result
        return result['value']

    def record(self, at):
        at=stamp(at)
        self.new_day(at)
        if not in_session(at,self.cfg) or self.last_tick_minute==at.isoformat()[:16]:
            return
        self.last_tick_minute=at.isoformat()[:16]
        payload={'values':{k:dict(v) for k,v in self.values.items()},'fut_code':self.fut_code,'segment':self.segment}
        self.store.tick(at.isoformat(),payload)
        self.history.append({'at':at.isoformat(), **payload})
        # 시장 가격은 하루 전 값으로 채우지 않음. 장중 부분 기록은 확정 종가로 표기하지 않음.
        columns=('k200','foreign_fut_qty','inst_fut_qty','foreign_spot_amt','nonarb_amt','basis','basis_residual',
                 'fut_price','fut_prev_close','fut_oi','fut_oi_prev')
        row={k:self.values.get(k,{}).get('value') for k in columns}
        row['observed_at']={k:self.values.get(k,{}).get('at') for k in columns}
        row.update({'fut_code':self.fut_code,'quality':'장중 잠정','finalized':False,'asof':at.isoformat()})
        prev=self.store.daily_row(self.day)
        if prev and prev.get('finalized'):
            # 이미 LS t1665 확정치로 고친 날(야간 등 늦은 기록)은 확정치를 덮지 않고 장중 값은 *_intraday에만 둔다.
            row.update({k:prev.get(k) for k in CONFIRMED_KEYS})
            row['foreign_fut_qty_intraday'],row['inst_fut_qty_intraday']=prev.get('foreign_fut_qty_intraday'),prev.get('inst_fut_qty_intraday')
        self.store.save_daily(self.day,row)

    def confirm_daily(self,hist):
        """LS t1665 일별 확정 순매수로 일별 DB의 외국인·기관 선물 값을 고친다. 고치기 전 장중 잠정값은 *_intraday로 남긴다
        (과거 장중 신호를 확정치로 다시 계산하면 미래 정보가 섞이므로 당시 값을 지운다는 뜻이 아니다)."""
        changed=[]
        with self.lock:
            for day,row in self.store.daily().items():
                q=(((hist.get('days') or {}).get(day) or {}).get('fut') or {}).get('qty') or {}
                f,i=q.get('foreign'),q.get('inst')
                if f is None or (row.get('finalized') and row.get('foreign_fut_qty')==f and row.get('inst_fut_qty')==i):
                    continue
                new=dict(row)
                if not row.get('finalized'):
                    new['foreign_fut_qty_intraday'],new['inst_fut_qty_intraday']=row.get('foreign_fut_qty'),row.get('inst_fut_qty')
                new.update({'foreign_fut_qty':f,'inst_fut_qty':i,'finalized':True,'quality':'LS t1665 일별 확정',
                            'confirmed_source':'LS t1665','confirmed_at':hist.get('updated_at')})
                self.store.save_daily(day,new);changed.append(day)
        return changed

    def cumulative(self,at):
        day=stamp(at).date()
        expiry=previous_futures_expiry(day,self.cfg)
        daily=self.store.daily()
        expected=expected_days(expiry+dt.timedelta(days=1),day,self.cfg)
        # 2026-10-23: 장중 잠정 저장값(daily.csv)은 마감 뒤 부호까지 바뀐 사례(10/22 −256 → 확정 +1,552)가 있어
        # LS t1665 일별 확정 이력이 있으면 그 값을 먼저 쓰고, 없을 때만 잠정값을 쓴다.
        try:
            import investor_history as H
            confirmed={d:v for d,v in H.series(H.load(self.store.directory),'fut','foreign')}
        except Exception:
            confirmed={}
        # 오늘은 장이 끝나 확정치가 들어오기 전까지 '누락'이 아니라 아직 포함하지 않는 날이다.
        expected=[d for d in expected if d<day.isoformat() or d in confirmed]
        vals,provisional,missing=[],[],[]
        for d in expected:
            if d in confirmed:vals.append(confirmed[d])
            elif d in daily and number(daily[d].get('foreign_fut_qty')) is not None:
                vals.append(daily[d]['foreign_fut_qty']);provisional.append(d)
            else:missing.append(d)
        complete=not missing and not self.calendar_warning
        return {'since':expiry.isoformat(),'complete':complete,'missing':missing,'observed_days':len(vals),'provisional':provisional,
            'sum':round(sum(vals),2) if vals else None,'label':('만기 이후 누적' if complete else '기록된 날짜만 부분 누적')+(' · 잠정 %d일 포함'%len(provisional) if provisional else '')+' (%s까지 · 오늘 장중 제외)'%(expected[-1] if expected else '-'),
            'note':'순매매 누적이며 보유 포지션이 아닙니다. 롤오버 거래는 별도 식별되지 않습니다. 확정치는 LS t1665 일별 이력.'}

    def options_view(self,at):
        def read(row,key):
            x=row.get(key)
            if not x:
                return None
            age=(stamp(at)-stamp(x['at'])).total_seconds()
            return x['value'] if 0<=age<=self.cfg['quality']['quote_ttl_sec'] else None
        rows=[{k:r.get(k) for k in ('code','strike','cp','month')} for r in self.options.values()]
        for out,r in zip(rows,self.options.values()):
            out.update({k:read(r,k) for k in ('price','oi','iv','delta')})
        rows.sort(key=lambda r:(r['strike'],r['cp']))
        spot=self.val('k200',at)
        atm=min(rows,key=lambda r:abs(r['strike']-spot))['strike'] if rows and spot else None
        ivs=[r['iv'] for r in rows if r['strike']==atm and r['iv'] is not None and r['iv']>0]
        top=sorted([r for r in rows if r['oi'] is not None],key=lambda r:r['oi'],reverse=True)[:4]
        return {'month':self.opt_month,'rows':rows,'atm':atm,'atm_iv':sum(ivs)/len(ivs) if ivs else None,
            'registered':len(rows),'fresh_oi':sum(r['oi'] is not None for r in rows),
            'top_oi':top,'note':'전체 시장 미결제. 콜·풋 미결제만으로 특정 투자자의 방향이나 지수 수렴을 판단하지 않습니다. 월물별 IV는 직접 비교하지 않습니다.'}

    def price_oi_view(self, at, metrics):
        """근월 선물 가격 변화와 시장 전체 미결제 변화를 5분·15분·전일 대비로 묶어 4분면에 놓는다."""
        po=price_oi_rules(self.cfg)
        price,prev_close=self.val('fut_price',at),self.val('fut_prev_close',at)
        if prev_close is None:
            # 전일 종가는 하루 내내 같은 값이라 시세 유효시간(45초)이 지나도 같은 날·같은 종목 값이면 쓴다(장외 화면에서 비던 문제).
            row=self.values.get('fut_prev_close') or {}
            if row.get('value') and row.get('at') and stamp(row['at']).date().isoformat()==self.day:prev_close=row['value']
        windows=[]
        for key,title in (('m5','최근 5분'),('m15','최근 15분'),('day','전일 대비')):
            if key=='day':
                pct=(price/prev_close-1)*100 if price and prev_close else None
                oi=self.val('fut_oi_prev',at)
            else:
                change=metrics['fut_price'][key]
                pct=None if change is None or not price or price-change<=0 else change/(price-change)*100
                oi=metrics['fut_oi'][key]
            # 당일(day)은 장 시작부터의 순매매 누적, 5·15분은 그 구간 변화.
            foreign,inst,spot=(metrics[k][key] for k in ('foreign_fut_qty','inst_fut_qty','foreign_spot_amt'))
            windows.append({'key':key,'title':title,'foreign_fut':foreign,
                **price_oi_quadrant(pct,oi,po['price_pct'][key],po['oi'][key]),
                'reading':reading(key,foreign,oi,pct,spot,inst,self.cfg,'당일' if key=='day' else title)})
        day=stamp(at).date()
        expiry=next_futures_expiry(day,self.cfg)
        left=len(expected_days(day+dt.timedelta(days=1),expiry,self.cfg)) if expiry else None
        # 만기 직전에는 근월물을 원월물로 옮기는 롤오버로 근월 미결제가 줄어 '청산'처럼 보인다.
        rollover='선물 만기(%s)까지 %d거래일 · 롤오버로 근월 미결제가 줄어 청산처럼 보일 수 있습니다.' % (expiry.isoformat(),left) if left is not None and left<=7 else ''
        return {'price':price,'prev_close':prev_close,'oi':self.val('fut_oi',at),'windows':windows,'thresholds':po,'rollover':rollover,
            'note':'시장 전체 미결제는 매수·매도 양쪽이 함께 늘고 줄어드는 값입니다. 4분면은 어느 쪽이 주도했는지의 모양일 뿐, 외국인 등 특정 투자자의 신규·청산을 뜻하지 않습니다. 움직임 기준은 검증 전 초기값입니다.'}

    def observe(self, kind, side, at, facts, meta):
        """옵션 분석 등 외부 관측을 기존 관측 기록에 넣어 5·15·30·60분 후 지수 변화를 같은 방식으로 평가(검토 6번).
        판정 시간 안이고 실시간 지수가 있을 때만, 같은 종류는 쿨다운 동안 한 번."""
        at=stamp(at)
        with self.lock:
            self.new_day(at)
            p=self.val('k200',at)
            if not p or not in_session(at,self.cfg,signals=True):
                return False
            saved=[e for e in self.store.events(200) if e['kind']==kind and e['at'][:10]==self.day]
            if saved and (at-stamp(saved[0]['at'])).total_seconds()<self.cfg['rules']['cooldown_min']*60:
                return False
            self.store.event({'id':uuid.uuid4().hex,'at':at.isoformat(),'kind':kind,'side':side,'price':p,'facts':facts,
                'rule_version':self.cfg['version'],'quality':'실험 규칙 / 검증 전','mode':self.mode,**meta,
                **event_metadata(self.cfg,self.mode,{'k200':self.values.get('k200',{})},meta)})
            return True

    def observation_inputs(self, at, delta_keys, current_keys):
        """Record only inputs used by this verdict, including its historical window."""
        observations=[]
        def add(key, values, role, history_at=None):
            row=values.get(key) or {}
            observations.append({**row,'field':key,'role':role,'history_at':history_at})
            if key=='basis_residual':
                # A derived label does not certify the provenance of its operands.
                for operand in ('basis','basis_theory'):
                    observations.append({**(values.get(operand) or {'provenance':'unknown'}),
                        'field':operand,'role':'derived_operand','history_at':history_at})
        for key in sorted(set(current_keys)|set(delta_keys)):
            add(key,self.values,'current')
        for key in sorted(set(delta_keys)):
            detail=getattr(self,'window_details',{}).get(key,{}).get('m5',{})
            selected=detail.get('base_observed_at')
            base=next((h for h in self.history if h['at']==selected),None)
            if base is None:
                observations.append({'field':key,'role':'window_base','provenance':'unknown'})
                continue
            for h in self.history:
                if stamp(base['at'])<=stamp(h['at'])<=at:
                    add(key,h['values'],'window_base' if h is base else 'window',h['at'])
        return observations

    def evaluate_events(self,at):
        # 신호 이후의 최초 관측을 사용. 해당 시각 가격이 없거나 큰 시간 공백이 있으면 평가 제외.
        for event in self.store.events(300):
            if event['at'][:10]!=self.day or not event.get('price'):
                continue
            results=event['outcomes']
            changed=False
            for mins in (5,15,30,60):
                if str(mins) in results:
                    continue
                target=stamp(event['at'])+dt.timedelta(minutes=mins)
                if target.date().isoformat()!=self.day or target.strftime('%H:%M')>self.cfg['session'].get('index_close','15:30'):
                    results[str(mins)]={'status':'장외'};changed=True;continue
                if target>stamp(at):
                    continue
                samples=[h for h in self.history if stamp(h['at'])>=target]
                if not samples:
                    continue
                h=samples[0]; m=h['values'].get('k200')
                if not m or (stamp(h['at'])-target).total_seconds()>75 or (stamp(h['at'])-stamp(m['at'])).total_seconds()>45:
                    results[str(mins)]={'status':'가격 누락'}
                else:
                    results[str(mins)]={'status':'관측','return_pct':round((m['value']/event['price']-1)*100,4),'at':h['at'],
                        'observed_at':m.get('at'),'source':m.get('source'),'quality':m.get('quality'),
                        'data_provenance':provenance([m],self.mode)}
                changed=True
            if changed:
                self.store.outcomes(event['id'],results)

    def compute(self, at=None):
        at=stamp(at or now_kst())
        with self.lock:
            self.new_day(at)
            self.window_details={}
            # 베이시스 잔차의 freshness는 두 원본 중 더 오래된 시각으로 제한.
            b,t=self.val('basis',at),self.val('basis_theory',at)
            if b is not None and t is not None:
                source_at=min(self.values['basis']['at'], self.values['basis_theory']['at'])
                self.values['basis_residual']={'value':b-t,'at':source_at,'source':'계산: 시장−이론','quality':'derived'}
            else:
                self.values.pop('basis_residual',None)
            self.record(at)
            keys=('foreign_fut_qty','inst_fut_qty','foreign_spot_amt','inst_spot_amt','nonarb_amt','program_amt','basis_residual','fut_oi','fut_price','k200')
            metrics={k:{'day':self.val(k,at), 'm1':self.delta(k,1,at),'m5':self.delta(k,5,at),'m15':self.delta(k,15,at)} for k in keys}
            p=self.val('k200',at); pd=metrics['k200']['m5']
            price5=None if pd is None or not p or p-pd<=0 else pd/(p-pd)*100
            rules=self.cfg['rules']
            def direction(v,t):
                return None if v is None else 1 if v>=t else -1 if v<=-t else 0
            f5=metrics['foreign_fut_qty']['m5']
            prog5=metrics['nonarb_amt']['m5']
            if prog5 is None:
                prog5=metrics['program_amt']['m5']
            cash5=prog5 if prog5 is not None else metrics['foreign_spot_amt']['m5']
            cash_key=('nonarb_amt' if metrics['nonarb_amt']['m5'] is not None else
                      'program_amt' if metrics['program_amt']['m5'] is not None else 'foreign_spot_amt')
            fd=direction(f5,rules['foreign_fut_5m'])
            cd=direction(cash5,rules['program_5m'] if prog5 is not None else rules['foreign_spot_5m'])
            price_dir=direction(price5,rules['price_5m_pct'])
            bd=direction(metrics['basis_residual']['m5'],rules['basis_change_5m'])
            facts=[]
            if f5 is not None:
                facts.append('외국인 선물 최근 5분 %+.0f계약 (당일 %+.0f계약)' % (f5, self.val('foreign_fut_qty',at)))
            fa,sa=self.val('foreign_fut_amt',at),self.val('foreign_spot_amt',at)
            if fa is not None and sa is not None:
                # 같은 억원 단위의 관측을 나란히 표시하고 거래 목적은 귀속하지 않는다.
                hedge=' → 현물·선물 반대 방향, 거래 목적은 식별 불가' if fa*sa<0 and min(abs(fa),abs(sa))>=100 else ''
                facts.append('외국인 당일 현물 %+.0f억 · 선물 %+.0f억%s'%(sa,fa,hedge))
            if cash5 is not None:
                facts.append('%s 최근 5분 %+.1f억원' % (NAMES[cash_key],cash5))
            if price5 is not None:
                facts.append('KOSPI200 최근 5분 %+.2f%%' % price5)
            if bd is not None:
                facts.append('시장−이론 베이시스 최근 5분 %+.2fp' % metrics['basis_residual']['m5'])
            available=sum(x is not None for x in (fd,cd,price_dir))
            ups=sum(x==1 for x in (fd,cd,price_dir)); downs=sum(x==-1 for x in (fd,cd,price_dir))
            side=0
            if p is None:
                label,guide='데이터 대기','지수 실시간 데이터부터 확인합니다.'
            elif not in_session(at,self.cfg,signals=True):
                label,guide='판정 시간 외','수급과 시세를 표시하며 장중 전환 알림은 보류합니다.'
            elif available<2:
                label,guide='관측 축적 / 연결 확인','5분 관측과 수급 데이터가 있어야 흐름을 비교할 수 있습니다.'
            elif fd is not None and cd is not None and fd*cd<0:
                label,guide='현물·선물 엇갈림','헤지·차익·롤오버 등 거래 목적을 구분할 수 없어 방향 해석을 보류합니다.'
            elif ups==3:
                label,guide,side='매수 흐름 동조','보유·관심종목의 가격과 거래량이 함께 회복하는지 확인합니다.',1
            elif downs==3:
                label,guide,side='매도 흐름 동조','시장 수급 압력이 이어지는지 관찰하며 종목별 대응을 점검합니다.',-1
            elif fd is None and cd and price_dir==cd:
                side=cd; label='현물 매수 유입' if cd==1 else '현물 매도 유입'
                guide='현물·프로그램과 지수의 동조입니다. 외국인 선물 데이터는 미연결 또는 지연 상태입니다.'
            else:
                label,guide='혼조 / 변화 관찰','수급과 가격의 방향이 충분히 일치하지 않습니다.'
            confirmations=[]
            if side:
                if self.candidate is None or self.candidate['side']!=side or self.candidate['label']!=label or (at-self.candidate['last']).total_seconds()>75:
                    self.candidate={'side':side,'label':label,'since':at,'last':at}
                else:
                    self.candidate['last']=at
                used_deltas=['k200']
                if f5 is not None:used_deltas.append('foreign_fut_qty')
                used_deltas.append('nonarb_amt' if metrics['nonarb_amt']['m5'] is not None else
                    'program_amt' if metrics['program_amt']['m5'] is not None else 'foreign_spot_amt')
                if bd is not None:used_deltas.append('basis_residual')
                used_current=['k200']+(['foreign_fut_amt','foreign_spot_amt'] if fa is not None and sa is not None else [])
                # Keep each verdict's actual axes during confirmation (fallback axes can change).
                cutoff=at-dt.timedelta(minutes=rules['confirm_min'])
                batches=[b for b in self.candidate.get('input_batches',[]) if b['at']>=cutoff]
                batches.append({'at':at,'inputs':self.observation_inputs(at,used_deltas,used_current)})
                self.candidate['input_batches']=batches
                held=(at-self.candidate['since']).total_seconds()/60
                confirmations.append('동조 관측 %.1f / %d분' % (held,rules['confirm_min']))
                last=self.last_event.get(label)
                saved=[e for e in self.store.events(100) if e['kind']==label and e['at'][:10]==self.day]
                if saved:
                    last=stamp(saved[0]['at'])
                if held>=rules['confirm_min'] and (last is None or (at-last).total_seconds()>=rules['cooldown_min']*60):
                    event={'id':uuid.uuid4().hex,'at':at.isoformat(),'kind':label,'side':side,'price':p,'facts':facts,
                        'rule_version':self.cfg['version'],'thresholds':rules,'quality':'실험 규칙 / 검증 전','mode':self.mode,
                        **event_metadata(self.cfg,self.mode,{},
                            {'input_observations':[m for b in batches for m in b['inputs']],
                              'rule_context':{'method':'engine-confluence-1','window_selection':'valid-predecessor-2'}})}
                    self.store.event(event); self.last_event[label]=at
            else:
                self.candidate=None
            turns=[]
            for key,title,threshold in [('foreign_fut_qty','외국인 선물',rules['foreign_fut_5m']),('nonarb_amt','비차익 프로그램',rules['program_5m'])]:
                recent=metrics[key]['m5']; prior=self.delta(key,5,at,offset=5)
                if recent is not None and prior is not None and recent>=threshold and prior<=-threshold:
                    turns.append(title+' 매도→매수 전환 관찰')
                elif recent is not None and prior is not None and recent<=-threshold and prior>=threshold:
                    turns.append(title+' 매수→매도 전환 관찰')
            price_oi=self.price_oi_view(at,metrics)
            explanation=build_explanation(self,at,metrics,price5,cash_key,(fd,cd,price_dir),label,guide,price_oi)
            rebound=rebound_view(self,at,metrics,price5)
            w15,wday=price_oi['windows'][1],price_oi['windows'][2]
            if wday['oi_change'] is not None:
                shape=' · 가격 %+.2f%% → %s' % (wday['price_pct'],wday['label']) if wday['price_pct'] is not None else ''
                facts.append('시장 전체 미결제 전일 대비 %+.0f계약%s. 투자자별 신규·청산 구분 불가' % (wday['oi_change'],shape))
            if w15['price_dir'] is not None and (w15['price_dir'] or w15['oi_dir']):
                facts.append('선물 가격·미결제 최근 15분 %+.2f%% · %+.0f계약 → %s' % (w15['price_pct'],w15['oi_change'],w15['label']))
            self.evaluate_events(at)
            flowrows=[]
            for inv,title in [('foreign','외국인'),('inst','기관계'),('financial','금융투자'),('individual','개인')]:
                fk=inv+'_fut_qty'; sk=inv+'_spot_amt'
                fut=self.measurement(fk,at);prev=self.manual_prev.get(fk)
                manual_change=None
                if self.manual(fut) and fut['fresh'] and prev:
                    manual_change={'value':fut['value']-prev['value'],'minutes':round((stamp(fut['at'])-stamp(prev['at'])).total_seconds()/60)}
                flowrows.append({'investor':title,'fut':fut,'fut_amt':self.measurement(inv+'_fut_amt',at), 'spot':self.measurement(sk,at),'fut_manual_change':manual_change,
                    'fut5':self.delta(fk,5,at),'spot5':self.delta(sk,5,at),'fut15':self.delta(fk,15,at),'spot15':self.delta(sk,15,at)})
            quality=[self.measurement(k,at) for k in LABELS]
            return {'version':self.cfg['version'],'mode':self.mode,'generated_at':at.isoformat(),'day':self.day,
                'session':in_session(at,self.cfg),'verdict':{'label':label,'side':side,'guide':guide,'facts':facts,'turns':turns,'confirmations':confirmations,
                    'note':'실험 규칙에 따른 관측 분류입니다. 예측 확률·매수 추천·목표 비중을 의미하지 않습니다.'},
                'metrics':metrics,'price5_pct':price5,'flows':flowrows,'quality':quality,'connections':dict(self.connections),
                'flow_explanation':explanation,
                'rebound':rebound,
                'window_details':self.window_details,
                'price_oi':price_oi,'options':self.options_view(at),'market_snapshots':dict(self.market_snapshots),'positions':scenarios(self.positions,p,at),'cumulative':self.cumulative(at),
                'stocks':[{**r,'fresh':bool(r.get('closing')) or 0<=(at-stamp(r['asof'])).total_seconds()<=self.cfg['rest']['stocks_poll_sec']*2} for r in self.stocks],
                'events':self.store.events(80),'calendar_warning':self.calendar_warning,
                'history':self.history_view(),
                'rules':rules}

    def history_view(self):
        out=[]
        for h in self.history[-450:]:
            values={}
            for key,m in h['values'].items():
                quote=key in QUOTE_KEYS
                ttl=self.cfg['quality']['quote_ttl_sec'] if quote else self.flow_ttl(m)
                age=(stamp(h['at'])-stamp(m['at'])).total_seconds()
                values[key]=m['value'] if 0<=age<=ttl else None
            out.append({'at':h['at'],'values':values})
        return out
